"""Seeding a world's starter voices from Kokoro presets, against a fake engine."""

import json
from pathlib import Path

import httpx
import pytest
from test_speech import wav

from swarmify_connectors import speech as sp
from swarmify_connectors import voices


def test_voice_names_reads_plain_names_and_kokoros_objects() -> None:
    assert sp.voice_names(["a", {"id": "b"}, {"name": "c"}, {"x": 1}, 3]) == {"a", "b", "c"}
    assert sp.voice_names(None) == set()


def kokoro(spoken: list[str], short: str = "") -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/voices":
            # Kokoro-FastAPI's real shape: objects, not plain names.
            listed = [
                {"id": v, "name": v, "overall_grade": "B"}
                for v in ("af_heart", "bm_fable", "bf_lily")
            ]
            return httpx.Response(200, json={"voices": listed})
        body = json.loads(request.content)
        spoken.append(body["voice"])
        assert body["input"] == voices.SENTENCE and body["response_format"] == "wav"
        return httpx.Response(200, content=wav([(3.0 if body["voice"] == short else 9.0, True)]))

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_seeding_writes_a_clip_and_an_entry_per_preset_both_engines_can_speak(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "assets" / "voices"
    folder.mkdir(parents=True)
    mine = {"name": "me", "description": "Me", "engine": "chatterbox", "clip": "me.wav",
            "consent": "My own recording."}  # fmt: skip
    old = {"name": "af_heart", "description": "old", "engine": "kokoro", "preset": "af_heart"}
    (folder / "voices.json").write_text(json.dumps([mine, old]))
    (folder / "me.wav").write_bytes(wav([(6.0, True)]))
    spoken: list[str] = []
    with kokoro(spoken, short="bf_lily") as http:
        skipped = voices.seed(http, "http://k", folder, ["af_heart", "bm_fable", "bf_lily", "zz"])
    assert spoken == ["af_heart", "bm_fable", "bf_lily"]
    assert skipped == [
        "bf_lily: the clip is under 5 s, too short to clone",
        "zz: not among the engine's voices",
    ]
    found, warnings = sp.catalog(str(tmp_path / "assets"))
    assert warnings == [] and [v["name"] for v in found] == ["me", "af_heart", "bm_fable"]
    fable = json.loads((folder / "voices.json").read_text())[2]
    assert fable == {
        "name": "bm_fable",
        "description": "Calm British storyteller",
        "tags": ["calm", "narrator", "british", "male"],
        "engine": "chatterbox",
        "clip": "bm_fable.wav",
        "preset": "bm_fable",
        "consent": "synthetic: Kokoro preset bm_fable, Apache 2.0",
    }
    assert (folder / "bm_fable.wav").is_file() and not (folder / "bf_lily.wav").exists()
    # the clip for Chatterbox and the preset for Kokoro: a fallback keeps the voice
    heart = found[1]
    assert sp.usable(heart, clips=True, presets=set()) and sp.usable(heart, False, {"af_heart"})


def test_the_default_set_is_varied_and_others_are_told_by_their_names() -> None:
    tags = [t for _, ts in voices.STARTERS.values() for t in ts]
    assert len(voices.STARTERS) == 8
    assert [tags.count(t) for t in ("warm", "bright", "deep")] == [2, 2, 2]
    assert "narrator" in tags and "british" in tags
    assert voices.described("bf_lily") == ("Kokoro preset bf_lily", ["british", "female"])


def test_the_command_names_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SWARMIFY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KOKORO_URL", "http://127.0.0.1:9")  # nothing listens on discard
    assert voices.main([]) == 2
    assert voices.main(["world_1"]) == 1 and "no world world_1" in capsys.readouterr().err
    (tmp_path / "worlds" / "world_1").mkdir(parents=True)
    monkeypatch.setattr(voices.shutil, "which", lambda _: None)
    assert voices.main(["world_1"]) == 1
    assert "Docker is unavailable" in capsys.readouterr().err


def test_the_operators_edits_write_the_catalog_the_connector_reads(tmp_path: Path) -> None:
    folder = tmp_path / "assets" / "voices"
    seeded = {"name": "af_heart", "description": "Warm", "engine": "chatterbox",
              "preset": "af_heart", "consent": "synthetic", "extra": 1}  # fmt: skip
    folder.mkdir(parents=True)
    (folder / "voices.json").write_text(json.dumps([seeded, "junk"]))
    me = voices.add(
        folder, "me", " My voice ", ["warm", " ", "warm", "me "], "Mine.", wav([(6, True)])
    )
    assert me == {"name": "me", "description": "My voice", "tags": ["warm", "me"],
                  "engine": "chatterbox", "clip": "me.wav", "consent": "Mine."}  # fmt: skip
    found, warnings = voices.listed(tmp_path / "assets")
    assert [(v["name"], v["clip"], v["seconds"]) for v in found] == [
        ("af_heart", None, None),
        ("me", "me.wav", 6.0),
    ]
    assert warnings == ["voice 2 in voices.json is ignored: it is not an object"]  # kept as is
    voices.edit(folder, "af_heart", "Warm and kind", ["kind"])
    entries = json.loads((folder / "voices.json").read_text())
    assert entries[0] == {**seeded, "description": "Warm and kind", "tags": ["kind"]}
    assert entries[1] == "junk" and not list(folder.glob(".*"))
    voices.remove(folder, "me")
    assert not (folder / "me.wav").exists()
    assert [e for e in json.loads((folder / "voices.json").read_text())] == [entries[0], "junk"]


@pytest.mark.parametrize(
    ("name", "consent", "clip", "says"),
    [
        ("my voice", "Mine.", wav([(6, True)]), "letters, digits"),
        ("me", " ", wav([(6, True)]), "consent"),
        ("me", "Mine.", wav([(3, True)]), "3.0 s; Chatterbox clones from 5 to 10 s"),
        ("me", "Mine.", wav([(11, True)]), "11.0 s"),
        ("me", "Mine.", b"ID3 not a wav", "16-bit WAV"),
        ("taken", "Mine.", wav([(6, True)]), "already named taken"),
    ],
)
def test_a_voice_is_refused_with_its_reason_and_nothing_written(
    tmp_path: Path, name: str, consent: str, clip: bytes, says: str
) -> None:
    folder = tmp_path / "voices"
    folder.mkdir()
    taken = {"name": "taken", "description": "x", "engine": "kokoro", "preset": "x",
             "consent": "x"}  # fmt: skip
    (folder / "voices.json").write_text(json.dumps([taken]))
    with pytest.raises(ValueError, match=says):
        voices.add(folder, name, "A voice", [], consent, clip)
    assert json.loads((folder / "voices.json").read_text()) == [taken]
    assert sorted(p.name for p in folder.iterdir()) == ["voices.json"]
    with pytest.raises(LookupError):
        voices.edit(folder, "nope", "x", [])
    with pytest.raises(ValueError, match="description"):
        voices.edit(folder, "taken", " ", [])
