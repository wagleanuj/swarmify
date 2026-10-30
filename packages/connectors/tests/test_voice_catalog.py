"""The Assets page's edits to a world's voice catalog, read back with the speech package's rules."""

import io
import json
import math
import wave
from pathlib import Path

import pytest

from swarmify_connectors import voices


def wav(parts: list[tuple[float, bool]], rate: int = 16000) -> bytes:
    """A 16-bit mono WAV of (seconds, loud) parts: a loud part is a tone, the rest silence."""
    samples = bytearray()
    for seconds, loud in parts:
        for i in range(int(seconds * rate)):
            value = int(12000 * math.sin(2 * math.pi * 220 * i / rate)) if loud else 0
            samples += value.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))
    return buf.getvalue()


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
