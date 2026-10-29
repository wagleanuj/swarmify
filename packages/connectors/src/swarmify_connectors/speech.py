"""The speech connector: a stdio MCP server that voices a script, one take per scene, and writes
the word timings and cut points a voice-first video is edited on (ADR 0015).

Engines are speech services outside the core (ADR 0019) speaking OpenAI's `/v1/audio/speech`,
listed in order in `SPEECH_ENDPOINTS`. A run tries them in that order; when one fails, the whole
run is redone on the next, so one video never mixes two voices. Word times come with the speech
when the engine offers Kokoro-FastAPI's `/dev/captioned_speech`, else from the engine's
`/v1/align`; either way they are matched back to the script, whose spelling wins.

`MANIFEST` holds everything Swarmify needs to know about this connector, in the shape of a V2
connector package's manifest (ADR 0019), so moving it into a package is a move, not a rewrite.
"""

import base64
import difflib
import io
import json
import math
import os
import re
import sys
import wave
from array import array
from itertools import accumulate
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

SCRIPT: dict[str, Any] = {
    "type": "object",
    "description": "A voice-over script: scenes, each voiced as one take, of spoken lines.",
    "properties": {
        "scenes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9_-]{1,60}$",
                        "description": "Unique; letters, digits, _ or -, as s01",
                    },
                    "lines": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "What is said. Directions in brackets:"
                                    " pauses (pause), (beat), (long pause), (pause 1.5s);"
                                    " delivery as [whispering] [happy] [angry] [sarcastic]"
                                    " [surprised] [fear] [crying] [dramatic] [narration]"
                                    " [advertisement] [laugh] [chuckle] [sigh] [gasp] [groan]"
                                    " [cough] [sniff] [shush] [clear throat], or screenplay"
                                    " words such as (whispers) or (laughs)",
                                },
                                "pause_after": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": "Seconds of silence after the line, 0.3 usually",
                                },
                            },
                            "required": ["text", "pause_after"],
                        },
                    },
                },
                "required": ["id", "lines"],
            },
        }
    },
    "required": ["scenes"],
}
"""The `script` type the connector declares (issue 133): a stage that writes its input promises
it with `output_type: script`. `parse_script` is the strict check of what arrives."""

MANIFEST: dict[str, Any] = {
    "name": "speech",
    "kind": "stage",
    "stage_types": ["connector"],
    "about": "Voices a script, one take per scene, with word timings and cut points. Needs a"
    " speech engine (README, Speech engines).",
    "command": [sys.executable, "-m", "swarmify_connectors.speech"],
    "credentials": [],
    "settings": ["SPEECH_ENDPOINTS"],
    "params": {
        "type": "object",
        "properties": {
            "voice": {"type": "string", "description": "the engine's voice, as af_heart"},
            "engines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "SPEECH_ENDPOINTS entries, in the order to try them",
            },
            "seed": {"type": "integer", "description": "for repeatable takes"},
        },
        "additionalProperties": False,
    },
    "inputs": [
        "the script: fenced JSON scenes and lines, else paragraphs and sentences; directions in"
        " brackets, as (pause), (beat), (whispers), (laughs) or [sigh]"
    ],
    "outputs": ["timings.json, with the takes in narration/ beside it"],
    "health": "speech_health",
    "types": {"script": SCRIPT},
    "input_types": ["script"],
}

DEFAULT_ENDPOINTS = "http://localhost:8881,http://localhost:8880"
PAUSE = 0.3
"""Seconds after a line when the script does not say."""
WINDOW = 0.01
"""A cut point is the middle of the quietest window this long between two lines."""
SILENCE = 0.02
"""RMS (of full scale, about -34 dBFS) under which that window counts as silence."""
# ponytail: one fixed threshold; per-engine noise floors if a quiet join gets flagged.
LEAD_IN, TAIL = 0.05, 0.15
"""Seconds kept before a take's first word and after its last."""

_ID = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
_FENCE = re.compile(r"```json[ \t]*\n(.*?)\n```", re.S)
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

mcp = MCPServer("speech")


def endpoints() -> list[str]:
    raw = os.environ.get("SPEECH_ENDPOINTS") or DEFAULT_ENDPOINTS
    return [e.strip().rstrip("/") for e in raw.split(",") if e.strip()]


def parse_script(text: str) -> list[dict[str, Any]]:
    """Scenes of lines from a script: the fenced JSON block (`scenes[].id`, `lines[].text`,
    optional `pause_after` seconds) when there is one, else each paragraph a scene and each
    sentence a line. Directions in the text are read (`directions`). Raises ValueError naming
    what is wrong."""
    fenced = _FENCE.search(text)
    if fenced is None:
        body = _FRONTMATTER.sub("", text)
        prose = "\n".join(line for line in body.splitlines() if not line.startswith("#"))
        paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", prose) if p.strip()]
        scenes = [
            {"id": f"scene_{i}", "lines": [{"text": s} for s in re.split(r"(?<=[.!?])\s+", p)]}
            for i, p in enumerate(paragraphs, 1)
        ]
    else:
        try:
            scenes = json.loads(fenced.group(1))["scenes"]
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"the script's JSON block has no readable scenes: {exc}") from exc
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("the script has no scenes")
    out: list[dict[str, Any]] = []
    for scene in scenes:
        sid = scene.get("id") if isinstance(scene, dict) else None
        if not isinstance(sid, str) or not _ID.match(sid) or any(s["id"] == sid for s in out):
            raise ValueError(f"each scene needs a unique id of letters, digits, _ or -: {sid!r}")
        lines = scene.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ValueError(f"scene {sid} has no lines")
        kept: list[dict[str, Any]] = []
        for n, line in enumerate(lines, 1):
            where = f"scene {sid}, line {n}"
            if not isinstance(line, dict) or not isinstance(line.get("text"), str):
                got = "a string" if isinstance(line, str) else json.dumps(line)[:60]
                raise ValueError(
                    f'{where}: expected {{"text": ..., "pause_after": ...}}, got {got}'
                )
            pause = line.get("pause_after", PAUSE)
            if not isinstance(pause, int | float) or isinstance(pause, bool) or pause < 0:
                raise ValueError(f"{where}: pause_after must be seconds >= 0, got {pause!r}")
            if not _directed(line["text"], float(pause), kept):
                raise ValueError(f"{where}: the text is empty")
        out.append({"id": sid, "lines": kept})
    return out


_DIRECTION = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_PAUSE_WORDS = re.compile(r"(long )?(?:pause|beat)(?: (\d+(?:\.\d+)?) ?s(?:ec(?:onds?)?)?)?")
BEAT, LONG_BEAT = 0.5, 1.2
"""Seconds of a `(pause)` or `(beat)`, and of a `(long pause)`."""
TAGS: dict[str, list[str]] = {
    "whispering": ["whispers", "whisper", "whispered"],
    "happy": ["excited", "excitedly", "cheerful", "cheerfully", "happily"],
    "angry": ["angrily"],
    "sarcastic": ["sarcastically"],
    "surprised": [],
    "fear": ["scared", "fearfully", "afraid"],
    "crying": ["sadly", "sad", "cries"],
    "dramatic": ["dramatically"],
    "narration": [],
    "advertisement": [],
    "laugh": ["laughs", "laughing"],
    "chuckle": ["chuckles", "chuckling"],
    "sigh": ["sighs", "sighing"],
    "gasp": ["gasps"],
    "groan": ["groans"],
    "cough": ["coughs"],
    "sniff": ["sniffs"],
    "shush": [],
    "clear throat": ["clears throat", "clears his throat", "clears her throat"],
}
"""The delivery tags a script may use, each with the screenplay words that mean it. An engine
hears a tag only when its `/health` lists it in `tags`."""
_TAG_OF = {w: tag for tag, words in TAGS.items() for w in (tag, *words)}
_TAG = re.compile(r"\[(" + "|".join(map(re.escape, TAGS)) + r")\]")


def _directed(text: str, pause: float, kept: list[dict[str, Any]]) -> bool:
    """Append `text`'s lines to `kept`: a pause direction mid-line splits it with that gap, at
    its end adds to its `pause` (at its start to the line before); a known delivery direction
    becomes its `[tag]` in `say`, anything else in brackets is spoken and `unrecognized`.
    False when it has no words to speak."""
    spoke = False
    chunk: list[str] = []
    odd: list[str] = []

    def flush(gap: float) -> None:
        nonlocal spoke, chunk, odd
        if any(not _TAG.fullmatch(w) for w in chunk):
            kept.append({"say": " ".join(chunk), "pause_after": gap, "unrecognized": odd})
            spoke, chunk, odd = True, [], []
        elif kept:  # no words: the tags and the gap belong to the line before
            kept[-1]["say"] = " ".join([kept[-1]["say"], *chunk])
            kept[-1]["pause_after"] += gap
            kept[-1]["unrecognized"] += odd
            chunk, odd = [], []

    for i, piece in enumerate(_DIRECTION.split(text)):
        said = " ".join(piece.split())
        words = said.lower().rstrip(".!,")
        if i % 2 == 0 or not said:
            chunk += piece.split()
        elif beat := _PAUSE_WORDS.fullmatch(words):
            flush(float(beat[2]) if beat[2] else LONG_BEAT if beat[1] else BEAT)
        elif words in _TAG_OF:
            chunk.append(f"[{_TAG_OF[words]}]")
        else:
            chunk += said.split()
            odd.append(said)
    flush(pause)
    for line in kept:
        line["text"] = " ".join(_TAG.sub("", line["say"]).split())
        line["directions"] = _TAG.findall(line["say"])
    return spoke


def for_engine(say: str, tags: set[str]) -> str:
    """A line as an engine hears it: the `[tag]`s it does not advertise taken out."""
    return " ".join(_TAG.sub(lambda m: m[0] if m[1] in tags else "", say).split())


def engine_tags(http: httpx.Client, base: str) -> set[str]:
    """The tags the engine at `base` advertises in its `/health` (`"tags": [...]`), else none."""
    try:
        tags = http.get(f"{base}/health").json().get("tags")
    except (httpx.HTTPError, ValueError, AttributeError):
        return set()
    return {t for t in tags if isinstance(t, str)} if isinstance(tags, list) else set()


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def match(
    want: list[str], heard: list[tuple[str, float, float]], duration: float
) -> list[tuple[float, float]]:
    """A (start, end) in seconds per script word from the words an engine heard or spoke: the
    script's spelling wins and the times come from the audio. Words not matched (missed, or
    heard merged as "Goodnight") share the gap between their matched neighbours evenly."""
    sm = difflib.SequenceMatcher(
        a=[_norm(w) for w in want], b=[_norm(h[0]) for h in heard], autojunk=False
    )
    times: list[tuple[float, float] | None] = [None] * len(want)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            times[a + k] = heard[b + k][1:]
    spans: list[tuple[float, float]] = []
    i = 0
    while i < len(want):
        if (span := times[i]) is not None:
            spans.append(span)
            i += 1
            continue
        j = next((k for k in range(i, len(want)) if times[k] is not None), len(want))
        start = spans[-1][1] if spans else 0.0
        end = nxt[0] if j < len(want) and (nxt := times[j]) else duration
        step = (end - start) / (j - i)
        spans += [(start + k * step, start + (k + 1) * step) for k in range(j - i)]
        i = j
    return spans


def decode(wav: bytes) -> tuple[array, int]:
    """The first channel's 16-bit samples and the rate. Counts the frames itself: a streamed
    WAV (Kokoro's) declares a bogus length. Raises ValueError."""
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"takes must be 16-bit WAV, got {8 * w.getsampwidth()}-bit")
            channels, rate, data = w.getnchannels(), w.getframerate(), w.readframes(2**31 - 1)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"the take is not a WAV file: {exc}") from exc
    samples = array("h", data[: len(data) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples[::channels], rate


def quietest(samples: array, rate: int, a: float, b: float) -> tuple[float, bool]:
    """The middle of the quietest `WINDOW` between `a` and `b` seconds, and whether it is
    silent. A gap shorter than the window has no silence: the cut goes in its middle."""
    n, step = max(1, round(rate * WINDOW)), max(1, rate // 1000)
    i0, i1 = int(a * rate), min(int(b * rate), len(samples))
    if i1 - i0 < n:
        return (a + b) / 2, False
    energy = list(accumulate((s * s for s in samples[i0:i1]), initial=0))
    best = min(range(0, i1 - i0 - n + 1, step), key=lambda i: energy[i + n] - energy[i])
    rms = math.sqrt((energy[best + n] - energy[best]) / n) / 32768
    return (i0 + best + n / 2) / rate, rms <= SILENCE


def take_timings(
    scene: dict[str, Any], wav: bytes, heard: list[tuple[str, float, float]]
) -> dict[str, Any]:
    """One take's entry in timings.json: its duration, and per line its words in Remotion's
    Caption shape (ms from the take's start, each word's text carrying the space before it),
    `cut_in` and `cut_out` seconds, and `joined_next` when it runs into the next line with no
    silence to cut in."""
    samples, rate = decode(wav)
    duration = len(samples) / rate
    words = [(li, w) for li, line in enumerate(scene["lines"]) for w in line["text"].split()]
    spans = match([w for _, w in words], heard, duration)
    lines: list[dict[str, Any]] = []
    for li, line in enumerate(scene["lines"]):
        idx = [i for i, (wl, _) in enumerate(words) if wl == li]
        captions = [
            {
                "text": (" " if i != idx[0] else "") + words[i][1],
                "startMs": round(spans[i][0] * 1000),
                "endMs": round(spans[i][1] * 1000),
                "timestampMs": None,
                "confidence": None,
            }
            for i in idx
        ]
        lines.append(
            {**line, "first": spans[idx[0]][0], "last": spans[idx[-1]][1], "words": captions}
        )
    lines[0]["cut_in"] = max(0.0, lines[0]["first"] - LEAD_IN)
    lines[-1]["cut_out"] = min(duration, lines[-1]["last"] + TAIL)
    for line in lines:
        line["joined_next"] = False
    for before, after in zip(lines, lines[1:], strict=False):
        cut, silent = quietest(samples, rate, before["last"], after["first"])
        before["cut_out"] = after["cut_in"] = round(cut, 3)
        before["joined_next"] = not silent
    keys = ("text", "pause_after", "directions", "unrecognized", "cut_in", "cut_out")
    keys += ("joined_next", "words")
    return {
        "scene": scene["id"],
        "file": f"narration/{scene['id']}.wav",
        "duration": round(duration, 3),
        "lines": [
            {k: round(ln[k], 3) if k.startswith("cut") else ln[k] for k in keys} for ln in lines
        ],
    }


def _take(
    http: httpx.Client, base: str, say: str, text: str, voice: str, seed: int | None
) -> tuple[bytes, list[tuple[str, float, float]]]:
    """One take of `say` from the engine at `base`, with the words of `text` (`say` without its
    tags) it spoke and when."""
    body: dict[str, Any] = {"model": "tts-1", "input": say, "response_format": "wav"}
    if voice:
        body["voice"] = voice
    if seed is not None:
        body["seed"] = seed
    r = http.post(f"{base}/dev/captioned_speech", json={**body, "stream": False})
    if r.status_code != 404:
        r.raise_for_status()
        answer = r.json()
        audio, stamps = base64.b64decode(answer["audio"]), answer["timestamps"]
    else:
        r = http.post(f"{base}/v1/audio/speech", json=body)
        r.raise_for_status()
        audio = r.content
        a = http.post(f"{base}/v1/align", data={"text": text}, files={"file": ("take.wav", audio)})
        a.raise_for_status()
        stamps = a.json()["timestamps"]
    return audio, [(str(t["word"]), float(t["start_time"]), float(t["end_time"])) for t in stamps]


def voice_run(
    http: httpx.Client,
    scenes: list[dict[str, Any]],
    engines: list[str],
    voice: str = "",
    seed: int | None = None,
) -> tuple[str, list[tuple[bytes, dict[str, Any]]]]:
    """Every scene's take and timings from the first engine that voices them all. Raises
    ToolError naming each engine's failure."""
    failures = []
    for base in engines:
        try:
            tags, takes = engine_tags(http, base), []
            for scene in scenes:
                say = " ".join(for_engine(line["say"], tags) for line in scene["lines"])
                text = " ".join(line["text"] for line in scene["lines"])
                audio, heard = _take(http, base, say, text, voice, seed)
                takes.append((audio, take_timings(scene, audio, heard)))
            return base, takes
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{base}: {type(exc).__name__}: {exc}")
    raise ToolError("no speech engine voiced the script. " + "; ".join(failures))


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(600, connect=5))


@mcp.tool(
    description="Voice a script, one take per scene, with word timings and cut points. The"
    ' script is a fenced JSON block {"scenes": [{"id", "lines": [{"text", "pause_after"}]}]}, else'
    " paragraphs and sentences. In a line's text, (pause), (beat), (long pause) or (pause 1.5s)"
    " is silence, and a delivery direction such as (whispers), (laughs), (sighs), (excited),"
    " (sarcastically) or a tag such as [clear throat] reaches engines that support it."
)
def speech(
    idempotency_key: str,
    output_path: str,
    input_path: str = "",
    input_paths: list[str] | None = None,
    voice: str = "",
    engines: list[str] | None = None,
    seed: int | None = None,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The script is `input` (or the first of `inputs`). Writes `output_path` (timings.json)
    and the takes in `narration/` beside it. `engines` reorders or narrows SPEECH_ENDPOINTS;
    it cannot name an endpoint outside it, since the script is sent there."""
    # ponytail: answers when done rather than as a long job; a few scenes take under a minute.
    del idempotency_key, frontmatter  # a rerun simply voices again; nothing is posted
    script = Path(input_path or (input_paths or [""])[0])
    if not script.is_file():
        raise ToolError(f"the speech stage needs an `input`, the script; {script} is no file")
    configured = endpoints()
    order = [e.rstrip("/") for e in engines] if engines else configured
    if foreign := [e for e in order if e not in configured]:
        raise ToolError(
            f"engines must be among SPEECH_ENDPOINTS ({', '.join(configured)}): {foreign}"
        )
    try:
        scenes = parse_script(script.read_text())
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    with _client() as http:
        engine, takes = voice_run(http, scenes, order, voice, seed)
    out = Path(output_path)
    folder = out.parent / "narration"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.wav"):
        old.unlink()
    for audio, timing in takes:
        (out.parent / timing["file"]).write_bytes(audio)
    timings = {
        "engine": engine,
        "voice": voice or None,
        "seed": seed,
        "takes": [t for _, t in takes],
    }
    out.write_text(json.dumps(timings, indent=1))
    joined = [
        f"{t['scene']} line {i + 1} runs into line {i + 2}"
        for t in timings["takes"]
        for i, line in enumerate(t["lines"])
        if line["joined_next"]
    ]
    seconds = round(sum(t["duration"] for t in timings["takes"]), 1)
    return {
        "output": str(out),
        "engine": engine,
        "takes": len(takes),
        "seconds": seconds,
        "joined": joined,
        "message": f"{len(takes)} takes, {seconds} s, from {engine}"
        + (f"; no silence to cut at: {', '.join(joined)}" if joined else ""),
    }


@mcp.tool(description="Whether each speech engine in SPEECH_ENDPOINTS answers its health check.")
def speech_health() -> dict[str, Any]:
    checks = []
    with httpx.Client(timeout=3) as http:
        for base in endpoints():
            try:
                http.get(f"{base}/health").raise_for_status()
                checks.append({"name": base, "ok": True, "message": "up"})
            except httpx.HTTPError as exc:
                checks.append(
                    {"name": base, "ok": False, "message": str(exc) or type(exc).__name__}
                )
    return {"checks": checks}


if __name__ == "__main__":
    mcp.run("stdio")
