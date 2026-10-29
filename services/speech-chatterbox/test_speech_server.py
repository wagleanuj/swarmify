"""The service's HTTP face with a fake Turbo: a reference clip conditions it once per clip, and a
request without one speaks its built-in voice. The real model is checked by test_speech_smoke."""

import io
import wave
from typing import Any

import pytest
import server
from fastapi.testclient import TestClient


class Turbo:
    """Records each conditioning and which voice each take was spoken in."""

    sr = 24000

    def __init__(self) -> None:
        self.conds: Any = "built-in"
        self.prepared: list[bytes] = []
        self.spoke: list[tuple[str, Any]] = []

    def prepare_conditionals(self, path: str) -> None:
        with open(path, "rb") as f:
            clip = f.read()
        if len(clip) < 10:
            raise AssertionError("Audio prompt must be longer than 5 seconds!")
        self.prepared.append(clip)
        self.conds = f"cloned {len(self.prepared)}"

    def generate(self, text: str) -> list[list[float]]:
        self.spoke.append((text, self.conds))
        return [[0.0, 0.5, -0.5, 1.5]]


@pytest.fixture
def turbo(monkeypatch: pytest.MonkeyPatch) -> Turbo:
    fake = Turbo()
    monkeypatch.setitem(server.models, "tts", fake)
    monkeypatch.setattr(server, "voices", {})
    return fake


def speech(client: TestClient, clip: bytes | None = None, **body: Any) -> bytes:
    fields = {"input": "Who sleeps?", "response_format": "wav", **body}
    if clip is None:
        r = client.post("/v1/audio/speech", json=fields)
    else:
        files = {"reference": ("ref.wav", clip, "audio/wav")}
        r = client.post("/v1/audio/speech", data=fields, files=files)
    assert r.status_code == 200, r.text
    return r.content


def test_health_says_it_takes_clips() -> None:
    assert TestClient(server.app).get("/health").json()["clips"] is True


def test_a_clip_conditions_turbo_once_and_no_clip_keeps_its_own_voice(turbo: Turbo) -> None:
    client = TestClient(server.app)  # no lifespan: the fake stands in for the models
    take = speech(client)
    with wave.open(io.BytesIO(take)) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnframes()) == (24000, 2, 4)
        assert w.readframes(4)[-2:] == (32767).to_bytes(2, "little")  # clipped to full scale
    speech(client, b"clip one, long enough")
    speech(client, b"clip one, long enough", model="tts-1")
    speech(client, b"clip two, long enough")
    speech(client)
    assert turbo.prepared == [b"clip one, long enough", b"clip two, long enough"]
    voices = [conds for _, conds in turbo.spoke]
    assert voices == ["built-in", "cloned 1", "cloned 1", "cloned 2", "built-in"]


def test_a_clip_turbo_cannot_use_is_refused(turbo: Turbo) -> None:
    client = TestClient(server.app)
    files = {"reference": ("ref.wav", b"short", "audio/wav")}
    r = client.post("/v1/audio/speech", data={"input": "Hi."}, files=files)
    assert r.status_code == 400 and "5 seconds" in r.json()["detail"]
    assert client.post("/v1/audio/speech", json={"input": " "}).status_code == 400
