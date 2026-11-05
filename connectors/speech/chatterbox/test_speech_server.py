"""The service's HTTP face with a fake Turbo and full model: a reference clip conditions each
once per clip, a request without one speaks its built-in voice, and model "chatterbox" takes the
full model with its delivery settings. The real model is checked by test_speech_smoke."""

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
        self.settings: list[dict[str, float]] = []

    def prepare_conditionals(self, path: str) -> None:
        with open(path, "rb") as f:
            clip = f.read()
        if len(clip) < 10:
            raise AssertionError("Audio prompt must be longer than 5 seconds!")
        self.prepared.append(clip)
        self.conds = f"cloned {len(self.prepared)}"

    def generate(self, text: str, **settings: float) -> list[list[float]]:
        self.spoke.append((text, self.conds))
        self.settings.append(settings)
        return [[0.0, 0.5, -0.5, 1.5]]


@pytest.fixture
def turbo(monkeypatch: pytest.MonkeyPatch) -> Turbo:
    fake = Turbo()
    monkeypatch.setitem(server.models, "tts", fake)
    monkeypatch.setattr(server, "voices", {})
    return fake


@pytest.fixture
def full(monkeypatch: pytest.MonkeyPatch, turbo: Turbo) -> Turbo:
    """The full ChatterboxTTS, as if a request had loaded it already."""
    fake = Turbo()
    fake.conds = "full built-in"
    monkeypatch.setitem(server.models, "full", fake)
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


def test_health_says_it_takes_clips_and_lists_its_models() -> None:
    health = TestClient(server.app).get("/health").json()
    assert health["clips"] is True and health["models"] == ["chatterbox-turbo", "chatterbox"]


def test_model_chatterbox_speaks_with_the_full_model_and_its_settings(
    turbo: Turbo, full: Turbo
) -> None:
    client = TestClient(server.app)
    settings = {"exaggeration": 1.6, "cfg_weight": 0.2, "temperature": 1.05}
    speech(client, b"a 32 s clip, long enough", model="chatterbox", **settings)
    speech(client, b"a 32 s clip, long enough", model="chatterbox", **settings)
    speech(client, b"a 32 s clip, long enough", temperature=0.7)  # Turbo: its own conditioning
    speech(client, model="chatterbox")
    assert full.prepared == [b"a 32 s clip, long enough"]
    assert [c for _, c in full.spoke] == ["cloned 1", "cloned 1", "full built-in"]
    assert full.settings == [settings, settings, {}]
    assert turbo.prepared == [b"a 32 s clip, long enough"]
    assert turbo.settings == [{"temperature": 0.7}]  # Turbo takes no exaggeration or cfg_weight


@pytest.mark.parametrize("bad", [{"cfg_weight": 0}, {"exaggeration": 2.5}, {"temperature": 0}])
def test_settings_out_of_range_are_refused(full: Turbo, bad: dict[str, float]) -> None:
    r = TestClient(server.app).post("/v1/audio/speech", json={"input": "Hi.", **bad})
    assert r.status_code == 422 and full.spoke == []


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


class _Has:
    def __init__(self, yes: bool) -> None:
        self.yes = yes

    def is_available(self) -> bool:
        return self.yes


class _Torch:
    def __init__(self, cuda: bool, mps: bool) -> None:
        self.cuda = _Has(cuda)
        self.backends = type("B", (), {"mps": _Has(mps)})()


@pytest.mark.parametrize(
    ("cuda", "mps", "where"),
    [(True, False, "cuda"), (False, True, "mps"), (False, False, "cpu"), (True, True, "cuda")],
)
def test_it_runs_on_an_nvidia_gpu_else_an_apple_one_else_the_cpu(
    cuda: bool, mps: bool, where: str
) -> None:
    assert server.device(_Torch(cuda, mps)) == where


def test_idle_unload_frees_the_models_and_the_next_take_reloads(
    monkeypatch: pytest.MonkeyPatch, full: Turbo
) -> None:
    """After an hour without a take both models and their voices go; a take loads Turbo again."""
    monkeypatch.setattr(server, "IDLE_UNLOAD", 3600.0)
    monkeypatch.setattr(server, "last_used", 1000.0)
    assert not server.unload_idle(now=1000.0 + 3599)
    assert server.unload_idle(now=1000.0 + 3601)
    assert "tts" not in server.models and "full" not in server.models and not server.voices
    assert not server.unload_idle(now=1000.0 + 9999)  # nothing left to free
    reloaded = Turbo()
    monkeypatch.setitem(server.models, "device", "cpu")
    monkeypatch.setattr(server, "turbo_model", lambda: server.models.setdefault("tts", reloaded))
    speech(TestClient(server.app))
    assert reloaded.spoke == [("Who sleeps?", "built-in")]


def test_idle_unload_waits_for_a_take_in_progress(
    monkeypatch: pytest.MonkeyPatch, turbo: Turbo
) -> None:
    monkeypatch.setattr(server, "IDLE_UNLOAD", 1.0)
    monkeypatch.setattr(server, "last_used", 0.0)
    with server.tts_lock:
        assert not server.unload_idle(now=100.0)
    assert server.unload_idle(now=100.0)
