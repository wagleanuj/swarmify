"""The speech connector: a stdio MCP server that voices a script, one take per scene, and writes
the word timings and cut points a voice-first video is edited on (ADR 0015).

Engines are speech services outside the core (ADR 0019) speaking OpenAI's `/v1/audio/speech`,
listed in order in `SPEECH_ENDPOINTS`. A run tries them in that order; when one fails, the whole
run is redone on the next, so one video never mixes two voices. Word times come with the speech
when the engine offers Kokoro-FastAPI's `/dev/captioned_speech`, else from the engine's
`/v1/align`; either way they are matched back to the script, whose spelling wins.

A world may keep a voice catalog in its assets, `voices/voices.json` (`catalog`): the connector
answers its `script` type for that world with a `voice` to choose from (`speech_types`), and
voices each script in the voice its stage or the script names, as each engine can (`resolve`).
Only this connector gives `voices/` a meaning; to the core the schema is opaque.

It is a Connector Package (ADR 0019): `swarmify-connector.toml` beside it is everything Swarmify
knows of it, including the engines as a Setting and a Service whose health the core polls.
"""

import base64
import difflib
import io
import json
import math
import os
import re
import sys
import tomllib
import wave
from array import array
from itertools import accumulate
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

MANIFEST: dict[str, Any] = tomllib.loads(
    (Path(__file__).parent / "swarmify-connector.toml").read_text()
)
SCRIPT: dict[str, Any] = MANIFEST["types"]["script"]
"""The `script` type the Manifest declares (issue 133): a stage that writes its input promises it
with `output_type: script`. `parse_script` is the strict check of what arrives."""

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


VOICES = "voices"
"""The folder of a world's assets holding its voice catalog, voices.json, and its clips."""


def catalog(assets_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The world's voices from `<assets>/voices/voices.json`, a list of `{name, description,
    tags, engine, clip?, preset?, consent}` (`clip` made a path), and a warning for each entry
    ignored: one without consent, without a clip in that folder or a preset, or named twice."""
    folder = Path(assets_path) / VOICES
    file = folder / "voices.json"
    if not assets_path or not file.is_file():
        return [], []
    try:
        raw = json.loads(file.read_text())
    except ValueError as exc:
        return [], [f"voices.json is not JSON, so no voice of it is used: {exc}"]
    if not isinstance(raw, list):
        return [], ["voices.json is not a list of voices, so none of it is used"]
    voices: list[dict[str, Any]] = []
    warnings = []
    for n, entry in enumerate(raw, 1):
        if problem := _unusable(entry, folder, voices):
            warnings.append(f"voice {n} in voices.json is ignored: {problem}")
            continue
        clip = entry.get("clip")
        voices.append(
            {
                **{k: entry[k].strip() for k in ("name", "description", "engine", "consent")},
                "tags": entry.get("tags") or [],
                "clip": str(folder / clip) if clip else None,
                "preset": entry.get("preset") or None,
            }
        )
    return voices, warnings


def _unusable(entry: Any, folder: Path, kept: list[dict[str, Any]]) -> str:
    def text(key: str) -> bool:
        return isinstance(entry.get(key), str) and bool(entry[key].strip())

    if not isinstance(entry, dict):
        return "it is not an object"
    name = entry.get("name")
    if not isinstance(name, str) or not _ID.match(name):
        return f"a name is letters, digits, _ or -, got {name!r}"
    if any(v["name"] == name for v in kept):
        return f"{name} is named twice"
    if not text("consent"):
        return f"{name} has no consent, the short statement of the right to use the voice"
    if missing := [k for k in ("description", "engine") if not text(k)]:
        return f"{name} has no {missing[0]}"
    tags = entry.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return f"{name}'s tags are not a list of words"
    clip, preset = entry.get("clip"), entry.get("preset")
    if clip and (not isinstance(clip, str) or Path(clip).name != clip or clip.startswith(".")):
        return f"{name}'s clip is not a file name in voices/: {clip!r}"
    if clip and not (folder / clip).is_file():
        return f"{name}'s clip {clip} is not in voices/"
    if preset is not None and not text("preset"):
        return f"{name}'s preset is not an engine voice name"
    return "" if clip or preset else f"{name} has neither a clip nor a preset"


def script_type(voices: list[dict[str, Any]]) -> dict[str, Any]:
    """The `script` type for a world with these voices: SCRIPT with an optional top-level
    `voice`, one of their names, each told by its description and tags so the judge picks."""
    if not voices:
        return SCRIPT
    told = "; ".join(
        f"{v['name']}: {v['description']}" + (f" ({', '.join(v['tags'])})" if v["tags"] else "")
        for v in voices
    )
    voice = {
        "type": "string",
        "enum": [v["name"] for v in voices],
        "description": "The voice that speaks the whole script, the one that fits it best: " + told,
    }
    return {**SCRIPT, "properties": {**SCRIPT["properties"], "voice": voice}}


def script_voice(text: str) -> str:
    """The `voice` a script's fenced JSON block names, else ""."""
    fenced = _FENCE.search(text)
    try:
        voice = json.loads(fenced.group(1)).get("voice") if fenced else None
    except (ValueError, AttributeError):
        return ""
    return voice.strip() if isinstance(voice, str) else ""


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


def _get(http: httpx.Client, url: str) -> dict[str, Any]:
    try:
        answer = http.get(url).json()
    except (httpx.HTTPError, ValueError):
        return {}
    return answer if isinstance(answer, dict) else {}


def _words(found: Any) -> set[str]:
    return {t for t in found if isinstance(t, str)} if isinstance(found, list) else set()


def voice_names(found: Any) -> set[str]:
    """The voice names an engine lists: plain names, or objects with an `id` or `name` as
    Kokoro-FastAPI's `/v1/audio/voices` answers (`{"voices": [{"id": "af_heart", ...}]}`)."""
    if not isinstance(found, list):
        return set()
    names = (
        v if isinstance(v, str) else v.get("id") or v.get("name")
        for v in found
        if isinstance(v, str | dict)
    )
    return {n for n in names if isinstance(n, str) and n}


def engine_tags(http: httpx.Client, base: str) -> set[str]:
    """The tags the engine at `base` advertises in its `/health` (`"tags": [...]`), else none."""
    return _words(_get(http, f"{base}/health").get("tags"))


def usable(voice: dict[str, Any], clips: bool, presets: set[str]) -> bool:
    """Whether an engine can speak a catalog voice: from its clip when it takes clips, or as
    its preset when the engine has that voice."""
    return bool(voice["clip"] and clips) or voice["preset"] in presets


def resolve(
    chosen: dict[str, Any], voices: list[dict[str, Any]], clips: bool, presets: set[str]
) -> dict[str, Any] | None:
    """The catalog voice that speaks on an engine: the chosen one when it can, else the one it
    can that shares the most tags with it, else None, the engine's default."""
    if usable(chosen, clips, presets):
        return chosen

    def shared(v: dict[str, Any]) -> int:
        return len(set(v["tags"]) & set(chosen["tags"]))

    can = [v for v in voices if usable(v, clips, presets)]
    best = max(can, key=shared, default=None)
    return best if best is not None and shared(best) > 0 else None


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
    http: httpx.Client,
    base: str,
    say: str,
    text: str,
    voice: str,
    seed: int | None,
    clip: bytes | None = None,
) -> tuple[bytes, list[tuple[str, float, float]]]:
    """One take of `say` from the engine at `base`, with the words of `text` (`say` without its
    tags) it spoke and when. A `clip` goes up with the speech request as `reference`."""
    body: dict[str, Any] = {"model": "tts-1", "input": say, "response_format": "wav"}
    if voice:
        body["voice"] = voice
    if seed is not None:
        body["seed"] = seed
    r = None
    if clip is None:
        r = http.post(f"{base}/dev/captioned_speech", json={**body, "stream": False})
    if r is not None and r.status_code != 404:
        r.raise_for_status()
        answer = r.json()
        audio, stamps = base64.b64decode(answer["audio"]), answer["timestamps"]
    else:
        if clip is None:
            r = http.post(f"{base}/v1/audio/speech", json=body)
        else:
            form = {k: str(v) for k, v in body.items()}
            files = {"reference": ("reference.wav", clip, "audio/wav")}
            r = http.post(f"{base}/v1/audio/speech", data=form, files=files)
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
    voices: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None, list[tuple[bytes, dict[str, Any]]]]:
    """Every scene's take and timings from the first engine that voices them all, and the
    voice that spoke. `voice` names one of the catalog's `voices`, resolved per engine
    (`resolve`), or else is the engine's own voice; "" is the engine's default. Raises ToolError
    naming each engine's failure."""
    voices = voices or []
    chosen = next((v for v in voices if v["name"] == voice), None)
    failures = []
    for base in engines:
        try:
            health = _get(http, f"{base}/health")
            tags, clips = _words(health.get("tags")), health.get("clips") is True
            spoke, own, clip = (voice or None), voice, None
            if chosen is not None:
                # an engine has a preset when it lists it, as Kokoro-FastAPI's voices do
                presets = (
                    voice_names(_get(http, f"{base}/v1/audio/voices").get("voices"))
                    if any(v["preset"] for v in voices)
                    else set()
                )
                speaker = resolve(chosen, voices, clips, presets)
                spoke, own = (speaker or {}).get("name"), ""
                if speaker and speaker["clip"] and clips:
                    clip = Path(speaker["clip"]).read_bytes()
                elif speaker:
                    own = speaker["preset"]
            takes = []
            for scene in scenes:
                say = " ".join(for_engine(line["say"], tags) for line in scene["lines"])
                text = " ".join(line["text"] for line in scene["lines"])
                audio, heard = _take(http, base, say, text, own, seed, clip)
                takes.append((audio, take_timings(scene, audio, heard)))
            return base, spoke, takes
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
    assets_path: str = "",
) -> dict[str, Any]:
    """The script is `input` (or the first of `inputs`). Writes `output_path` (timings.json)
    and the takes in `narration/` beside it. `engines` reorders or narrows SPEECH_ENDPOINTS;
    it cannot name an endpoint outside it, since the script is sent there. The voice is the
    stage's `voice`, else the script's, else the engine's default; timings.json records the
    one that spoke."""
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
    text = script.read_text()
    try:
        scenes = parse_script(text)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    voices, warnings = catalog(assets_path)
    asked = voice.strip() or script_voice(text)
    if not voice.strip() and asked and all(v["name"] != asked for v in voices):
        warnings.append(f"the script's voice {asked} is not in the world's catalog")
        asked = ""
    with _client() as http:
        engine, spoke, takes = voice_run(http, scenes, order, asked, seed, voices)
    out = Path(output_path)
    folder = out.parent / "narration"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.wav"):
        old.unlink()
    for audio, timing in takes:
        (out.parent / timing["file"]).write_bytes(audio)
    timings = {
        "engine": engine,
        "voice": spoke,
        "chosen": asked or None,
        "seed": seed,
        "warnings": warnings,
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
    said = f" in {spoke}" if spoke else ""
    if asked and spoke != asked:
        said += f" (chosen {asked}, which {engine} cannot speak)"
    return {
        "output": str(out),
        "engine": engine,
        "voice": spoke,
        "takes": len(takes),
        "seconds": seconds,
        "joined": joined,
        "warnings": warnings,
        "message": f"{len(takes)} takes, {seconds} s, from {engine}{said}"
        + (f"; no silence to cut at: {', '.join(joined)}" if joined else "")
        + (f"; {'; '.join(warnings)}" if warnings else ""),
    }


@mcp.tool(
    description="The script type for one world: with a voice catalog in its assets, a `voice`"
    " to choose from among its voices."
)
def speech_types(world: str = "", assets_path: str = "") -> dict[str, Any]:
    """What the registry asks at check, run start and projection (issue 140)."""
    del world  # the assets folder is all the catalog needs
    voices, warnings = catalog(assets_path)
    names = ", ".join(v["name"] for v in voices)
    note = (
        f"The world has voices for speech: {names}. The judge of the swarm stage writing the"
        " script picks one per script (its `voice`) unless the speech stage fixes one with"
        " params.voice."
        if voices
        else ""
    )
    return {"types": {"script": script_type(voices)}, "note": note, "warnings": warnings}


if __name__ == "__main__":
    mcp.run("stdio")
