"""The speech connector against fake engines: order and fallback, words matched to the script,
cut points and the flag for joins with no silence, and the built-in started over stdio MCP."""

import base64
import io
import json
import math
import re
import threading
import wave
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import BUILTINS, Connectors, FakeTransport, HealthCheck, McpTransport
from swarmify_connectors import speech as sp
from swarmify_connectors.credentials import Credentials
from swarmify_connectors.sqlite import SqliteStorage

RATE = 16000
SCRIPT = """# The hedgehog

```json
{"scenes": [
  {"id": "hook", "lines": [{"text": "Who sleeps?", "pause_after": 0.5}, {"text": "Good night."}]},
  {"id": "fact", "lines": [{"text": "A hedgehog."}]}
]}
```
"""


def wav(parts: list[tuple[float, bool]]) -> bytes:
    """A 16-bit mono WAV of (seconds, loud) parts: a loud part is a tone, the rest silence."""
    samples = bytearray()
    for seconds, loud in parts:
        for i in range(int(seconds * RATE)):
            value = int(12000 * math.sin(2 * math.pi * 220 * i / RATE)) if loud else 0
            samples += value.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(samples))
    return buf.getvalue()


# Two lines with a 0.3 s silence between them: words 0-0.5, 0.5-1.0 | 1.3-1.6, 1.6-2.0.
SPOKEN = wav([(1.0, True), (0.3, False), (0.7, True), (0.2, False)])
WORDS = [("who", 0.0, 0.5), ("sleeps", 0.5, 1.0), ("Good", 1.3, 1.6), ("night", 1.6, 2.0)]


def kokoro(seen: list[str], fail: bool = False) -> Callable[[httpx.Request], httpx.Response]:
    """A captioned engine: its speech carries its own word times, punctuation tokens included."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})  # no tags
        body = json.loads(request.content)
        seen.append(f"{request.url.host} {request.url.path} {body['input']}")
        if fail and "hedgehog" in body["input"]:
            return httpx.Response(500, text="out of memory")
        stamps = [*WORDS[:2], ("?", 1.0, 1.3), *WORDS[2:], (".", 2.0, 2.1)]
        return httpx.Response(
            200,
            json={
                "audio": base64.b64encode(SPOKEN).decode(),
                "timestamps": [{"word": w, "start_time": s, "end_time": e} for w, s, e in stamps],
            },
        )

    return handle


def chatterbox(
    seen: list[str], said: list[str] | None = None, fail: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    """An engine without captions that advertises tags: speech, then its own alignment endpoint.
    `said` gets each speech input and each text it aligns."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "tags": ["whispering", "laugh"]})
        seen.append(f"{request.url.host} {request.url.path}")
        if request.url.path == "/dev/captioned_speech":
            return httpx.Response(404)
        if request.url.path == "/v1/audio/speech":
            if said is not None:
                said.append(json.loads(request.content)["input"])
            return httpx.Response(500) if fail else httpx.Response(200, content=SPOKEN)
        if said is not None:
            said.append(request.content.split(b'name="text"\r\n\r\n')[1].split(b"\r\n")[0].decode())
        # heard "Goodnight" as one word
        heard = [*WORDS[:2], ("Goodnight", 1.3, 2.0)]
        return httpx.Response(
            200,
            json={"timestamps": [{"word": w, "start_time": s, "end_time": e} for w, s, e in heard]},
        )

    return handle


def engines(**by_host: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: by_host[r.url.host](r)))


def line(text: str, pause: float = sp.PAUSE, say: str = "", **more: list[str]) -> dict[str, Any]:
    return {
        "text": text,
        "say": say or text,
        "pause_after": pause,
        "directions": more.get("directions", []),
        "unrecognized": more.get("unrecognized", []),
    }


def scene_of(lines: str) -> list[dict[str, Any]]:
    """The lines of a one-scene fenced script whose `lines` JSON is `lines`."""
    body = json.dumps({"scenes": [{"id": "s01", "lines": json.loads(lines)}]})
    return sp.parse_script(f"```json\n{body}\n```")[0]["lines"]


def test_the_fenced_json_gives_scenes_and_lines() -> None:
    scenes = sp.parse_script(SCRIPT)
    assert [s["id"] for s in scenes] == ["hook", "fact"]
    assert scenes[0]["lines"] == [line("Who sleeps?", 0.5), line("Good night.")]


def test_a_line_of_the_wrong_shape_is_named_with_the_shape_expected() -> None:
    # the script that failed run_01M6329GYYWZE12C55T0HKSBXV: lines as plain strings
    script = (Path(__file__).parent / "fixtures/kidney_script.md").read_text()
    with pytest.raises(ValueError) as err:
        sp.parse_script(script)
    assert str(err.value) == (
        'scene s01, line 1: expected {"text": ..., "pause_after": ...}, got a string'
    )


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ('[{"text": "Hi."}, {"text": " "}]', "scene s01, line 2: the text is empty"),
        (
            '[{"text": "Hi."}, {"text": "Bye.", "pause_after": -1}]',
            "scene s01, line 2: pause_after",
        ),
        ('[{"text": 3}]', 'scene s01, line 1: expected {"text": ..., "pause_after": ...}'),
        ('[{"text": "(beat)"}]', "scene s01, line 1: the text is empty"),
    ],
)
def test_a_bad_line_is_named(lines: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        scene_of(lines)


@pytest.mark.parametrize(
    ("text", "pauses"),
    [
        ("Usually... nothing. (pause)", [sp.PAUSE + 0.5]),
        ("Usually... nothing. (beat)", [sp.PAUSE + 0.5]),
        ("Usually... nothing. (long pause)", [sp.PAUSE + 1.2]),
        ("Usually... nothing. (pause 1.5s)", [sp.PAUSE + 1.5]),
        ("Usually... (beat) nothing.", [0.5, sp.PAUSE]),
        ("Usually... (pause 2 s) nothing.", [2.0, sp.PAUSE]),
        ("Usually... (long beat) nothing. (pause)", [1.2, sp.PAUSE + 0.5]),
    ],
)
def test_pauses_become_silence_ending_or_splitting_a_line(text: str, pauses: list[float]) -> None:
    lines = scene_of(json.dumps([{"text": text}]))
    assert [ln["pause_after"] for ln in lines] == pytest.approx(pauses)
    assert " ".join(ln["text"] for ln in lines) == "Usually... nothing."
    assert all(ln["directions"] == [] for ln in lines)


def test_a_pause_opening_a_line_lengthens_the_one_before() -> None:
    assert [
        ln["pause_after"] for ln in scene_of('[{"text": "One."}, {"text": "(beat) Two."}]')
    ] == [
        pytest.approx(sp.PAUSE + 0.5),
        sp.PAUSE,
    ]


def test_directions_map_to_tags_and_the_rest_is_spoken() -> None:
    lines = scene_of(
        json.dumps(
            [
                {"text": "(whispers) I know a secret. (laughs)"},
                {"text": "[clear throat] Ahem, (points at the screen) look. (Sarcastically) Fine."},
            ]
        )
    )
    assert lines == [
        line(
            "I know a secret.",
            say="[whispering] I know a secret. [laugh]",
            directions=["whispering", "laugh"],
        ),
        line(
            "Ahem, points at the screen look. Fine.",
            say="[clear throat] Ahem, points at the screen look. [sarcastic] Fine.",
            directions=["clear throat", "sarcastic"],
            unrecognized=["points at the screen"],
        ),
    ]


@pytest.mark.parametrize(
    ("words", "tag"),
    [
        ("whispering", "whispering"),
        ("excited", "happy"),
        ("cheerful", "happy"),
        ("happily", "happy"),
        ("dramatically", "dramatic"),
        ("sadly", "crying"),
        ("angrily", "angry"),
        ("surprised", "surprised"),
        ("scared", "fear"),
        ("fearfully", "fear"),
        ("chuckles", "chuckle"),
        ("sighs", "sigh"),
        ("gasps", "gasp"),
        ("laughing", "laugh"),
        ("[advertisement]", "advertisement"),
        ("[narration]", "narration"),
        ("[groan]", "groan"),
        ("[cough]", "cough"),
        ("[sniff]", "sniff"),
        ("[shush]", "shush"),
    ],
)
def test_screenplay_words_and_bracket_tags_are_known(words: str, tag: str) -> None:
    text = words if words.startswith("[") else f"({words.title()})"
    (only,) = scene_of(json.dumps([{"text": f"{text} Yes."}]))
    assert only["directions"] == [tag] and only["say"] == f"[{tag}] Yes."


def test_an_engine_hears_only_the_tags_it_advertises() -> None:
    say = "[whispering] I know a secret. [laugh]"
    assert sp.for_engine(say, {"whispering", "laugh"}) == say
    assert sp.for_engine(say, {"laugh"}) == "I know a secret. [laugh]"
    assert sp.for_engine(say, set()) == "I know a secret."


def test_plain_prose_is_paragraphs_of_sentences() -> None:
    text = "---\ntype: script\n---\n# Title\n\nWho sleeps? All winter\nlong.\n\nGood night!\n"
    assert sp.parse_script(text) == [
        {"id": "scene_1", "lines": [line("Who sleeps?"), line("All winter long.")]},
        {"id": "scene_2", "lines": [line("Good night!")]},
    ]


@pytest.mark.parametrize(
    "bad",
    [
        '```json\n{"scenes": [{"id": "../x", "lines": [{"text": "Hi"}]}]}\n```',
        '```json\n{"scenes": [{"id": "a", "lines": [{"text": "Hi", "pause_after": -1}]}]}\n```',
        '```json\n{"scenes": [{"id": "a", "lines": []}]}\n```',
        "```json\nnot json\n```",
        "# only a title\n",
    ],
)
def test_a_script_it_cannot_voice_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        sp.parse_script(bad)


def test_words_are_matched_back_to_the_script_spelling() -> None:
    want = ["Who", "sleeps?", "Good", "night."]
    heard = [("who", 0.0, 0.5), ("sleeps", 0.5, 1.0), ("Goodnight", 1.3, 2.0)]
    # "Goodnight" matches neither word: the two share the gap from 1.0 to the end
    assert sp.match(want, heard, 2.2) == [(0.0, 0.5), (0.5, 1.0), (1.0, 1.6), (1.6, 2.2)]


def test_the_first_engine_voices_every_scene_with_its_own_word_times() -> None:
    seen: list[str] = []
    with engines(a=kokoro(seen), b=chatterbox(seen)) as http:
        engine, takes = sp.voice_run(
            http, sp.parse_script(SCRIPT), ["http://a", "http://b"], "af_heart"
        )
    assert engine == "http://a" and len(seen) == 2  # one call per scene, b never asked
    hook = takes[0][1]
    assert hook["file"] == "narration/hook.wav" and hook["duration"] == 2.2
    first, second = hook["lines"]
    assert [w["text"] for w in first["words"]] == ["Who", " sleeps?"]
    assert [w["text"] for w in second["words"]] == ["Good", " night."]
    assert second["words"][1] == {
        "text": " night.", "startMs": 1600, "endMs": 2000, "timestampMs": None, "confidence": None
    }  # fmt: skip
    assert first["pause_after"] == 0.5 and second["pause_after"] == sp.PAUSE


def test_a_failing_engine_redoes_the_whole_run_on_the_next_so_voices_never_mix() -> None:
    seen: list[str] = []
    with engines(a=kokoro(seen, fail=True), b=chatterbox(seen)) as http:
        engine, takes = sp.voice_run(http, sp.parse_script(SCRIPT), ["http://a", "http://b"])
    assert engine == "http://b"
    assert [s.split()[:2] for s in seen[:2]] == [["a", "/dev/captioned_speech"]] * 2
    # b voices both scenes again, the hook included, through speech then alignment
    assert seen[2:] == ["b /dev/captioned_speech", "b /v1/audio/speech", "b /v1/align"] * 2
    assert [t["scene"] for _, t in takes] == ["hook", "fact"]


def test_when_every_engine_fails_the_error_names_each() -> None:
    seen: list[str] = []
    with engines(a=kokoro(seen, fail=True)) as http, pytest.raises(ToolError) as err:
        sp.voice_run(http, sp.parse_script(SCRIPT), ["http://a", "http://c"])
    assert "http://a: HTTPStatusError" in str(err.value) and "http://c:" in str(err.value)


TAGGED = """```json
{"scenes": [{"id": "hook", "lines": [
  {"text": "(whispers) Who sleeps? (beat) (sighs)"}, {"text": "Good (looks up) night."}
]}]}
```"""


def test_an_engine_that_advertises_tags_hears_them_but_alignment_and_captions_do_not() -> None:
    seen: list[str] = []
    said: list[str] = []
    with engines(b=chatterbox(seen, said)) as http:
        _, takes = sp.voice_run(http, sp.parse_script(TAGGED), ["http://b"])
    assert said == [
        "[whispering] Who sleeps? Good looks up night.",  # [sigh] is not among b's tags
        "Who sleeps? Good looks up night.",
    ]
    first, second = takes[0][1]["lines"]
    assert [w["text"] for w in first["words"]] == ["Who", " sleeps?"]
    assert first["directions"] == ["whispering", "sigh"] and first["unrecognized"] == []
    assert first["pause_after"] == 0.5 + sp.PAUSE
    assert second["text"] == "Good looks up night." and second["unrecognized"] == ["looks up"]
    assert "say" not in second


def test_a_fallback_to_an_engine_without_tags_strips_them() -> None:
    seen: list[str] = []
    said: list[str] = []
    kokoro_seen: list[str] = []
    with engines(b=chatterbox(seen, said, fail=True), a=kokoro(kokoro_seen)) as http:
        engine, _ = sp.voice_run(http, sp.parse_script(TAGGED), ["http://b", "http://a"])
    assert engine == "http://a"
    assert said == ["[whispering] Who sleeps? Good looks up night."]
    assert kokoro_seen == ["a /dev/captioned_speech Who sleeps? Good looks up night."]


def test_a_cut_lands_in_the_silence_between_lines() -> None:
    scene = sp.parse_script(SCRIPT)[0]
    take = sp.take_timings(scene, SPOKEN, WORDS)
    first, second = take["lines"]
    assert first["cut_in"] == 0.0 and second["cut_out"] == 2.15  # a 0.15 s tail kept
    assert 1.0 < first["cut_out"] == second["cut_in"] < 1.3
    assert first["joined_next"] is False


def test_lines_with_no_silence_between_them_are_flagged() -> None:
    scene = sp.parse_script(SCRIPT)[0]
    take = sp.take_timings(scene, wav([(2.0, True), (0.2, False)]), WORDS)
    assert take["lines"][0]["joined_next"] is True and take["lines"][1]["joined_next"] is False
    # a gap shorter than the window has no silence either: the cut goes in its middle
    touching = [*WORDS[:2], ("Good", 1.0, 1.6), WORDS[3]]
    first, _ = sp.take_timings(scene, SPOKEN, touching)["lines"]
    assert first["joined_next"] is True and first["cut_out"] == 1.0


def test_a_streamed_wav_with_a_bogus_length_is_read_to_its_end() -> None:
    raw = bytearray(SPOKEN)
    raw[40:44] = (2**31 - 1).to_bytes(4, "little")  # the data chunk's size, as Kokoro sends it
    samples, rate = sp.decode(bytes(raw))
    assert rate == RATE and len(samples) == int(2.2 * RATE)


def test_the_built_in_is_declared_by_its_manifest() -> None:
    builtin = next(c for c in BUILTINS if c.name == "speech")
    m = sp.MANIFEST
    assert (builtin.kind, builtin.command, builtin.stage_types) == (
        m["kind"],
        m["command"],
        m["stage_types"],
    )
    assert builtin.health == m["health"] == "speech_health"
    tool = next(t for t in sp.mcp._tool_manager.list_tools() if t.name == "speech")
    assert set(m["params"]["properties"]) <= set(tool.parameters["properties"])


def test_a_speech_stage_is_checked_for_its_script_output_and_params(
    connectors: Connectors,
) -> None:
    ok = {"calls": "speech", "input": "script.md", "output": "timings.json", "params": {"seed": 7}}
    assert connectors.stage_problems("w", ok) == []
    assert connectors.stage_problems("w", {"calls": "speech", "params": {"speed": 2}}) == [
        f"speech needs an input, {sp.MANIFEST['inputs'][0]}",
        f"speech needs an output, {sp.MANIFEST['outputs'][0]}",
        "speech does not take params.speed",
    ]


def test_a_stage_is_checked_against_its_manifest_not_its_name(
    connectors: Connectors, monkeypatch: pytest.MonkeyPatch
) -> None:
    # what #111 moves into a package: the core reads the rules from the manifest alone
    monkeypatch.setitem(sp.MANIFEST, "inputs", ["the words"])
    monkeypatch.setitem(sp.MANIFEST, "outputs", ["voice.json"])
    monkeypatch.setitem(sp.MANIFEST["params"], "properties", {"speed": {"type": "number"}})
    assert connectors.stage_problems("w", {"calls": "speech", "params": {"seed": 1}}) == [
        "speech needs an input, the words",
        "speech needs an output, voice.json",
        "speech does not take params.seed",
    ]


def test_a_built_in_gets_the_settings_its_manifest_declares(
    connectors: Connectors, monkeypatch: pytest.MonkeyPatch
) -> None:
    speech = connectors.find("w", "speech")
    assert speech is not None
    assert sp.MANIFEST["settings"] == ["SPEECH_ENDPOINTS"]
    monkeypatch.setenv("SPEECH_ENDPOINTS", "http://a")
    monkeypatch.setenv("OTHER_SETTING", "x")
    assert connectors._env(speech) == {"SPEECH_ENDPOINTS": "http://a"}
    monkeypatch.setitem(sp.MANIFEST, "settings", ["OTHER_SETTING"])
    assert connectors._env(speech) == {"OTHER_SETTING": "x"}


def test_health_lists_each_engine(connectors: Connectors, transport: FakeTransport) -> None:
    transport.outputs["speech_health"] = [
        {"checks": [{"name": "http://a", "ok": True, "message": "up"}]}
    ]
    speech = connectors.find("w", "speech")
    assert speech is not None
    assert connectors.health(speech) == [HealthCheck("http://a", True, "up")]
    youtube = connectors.find("w", "youtube")
    assert youtube is not None and connectors.health(youtube) == []


# Over stdio MCP, against a fake engine on a real port.


class Engine(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, b'{"status": "ok"}')

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["content-length"]))
        stamps = [{"word": w, "start_time": s, "end_time": e} for w, s, e in WORDS]
        audio = base64.b64encode(SPOKEN).decode()
        self._send(200, json.dumps({"audio": audio, "timestamps": stamps}).encode())


@pytest.fixture
def engine() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_built_in_voices_a_script_over_stdio(
    tmp_path: Path, db: Path, engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = "http://127.0.0.1:9"  # nothing listens on discard
    monkeypatch.setenv("SPEECH_ENDPOINTS", f"{down},{engine}")
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    script = tmp_path / "script.md"
    script.write_text(SCRIPT)
    out = tmp_path / "speak" / "artifacts" / "timings.json"
    out.parent.mkdir(parents=True)
    config = {
        "calls": "speech",
        "input": "script.md",
        "output": "timings.json",
        "params": {"input_path": str(script), "output_path": str(out), "seed": 7},
    }
    call = real.run_stage("w", "run_1", "speak", config)
    assert call.status == "done" and call.response is not None
    assert call.response["engine"] == engine and call.response["takes"] == 2
    timings = json.loads(out.read_text())
    assert [t["file"] for t in timings["takes"]] == ["narration/hook.wav", "narration/fact.wav"]
    assert (out.parent / "narration" / "hook.wav").read_bytes() == SPOKEN
    assert timings["seed"] == 7

    speech = real.find("w", "speech")
    assert speech is not None
    checks = real.health(speech)
    assert [(c.name, c.ok) for c in checks] == [(down, False), (engine, True)]
