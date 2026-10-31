"""Smoke test against the running speech engines (README, Speech engines); skips each one that is
not up. `SPEECH_ENDPOINTS` overrides the default list."""

import os

import httpx
import pytest

ENDPOINTS = os.environ.get("SPEECH_ENDPOINTS", "http://localhost:8881,http://localhost:8880")
TEXT = "The hedgehog sleeps all winter."


def _up(base: str) -> None:
    try:
        httpx.get(f"{base}/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"no speech engine at {base}")


@pytest.mark.parametrize("base", ENDPOINTS.split(","))
def test_engine_speaks_and_its_alignment_finds_the_words(base: str):
    _up(base)
    r = httpx.post(
        f"{base}/v1/audio/speech",
        json={"model": "tts-1", "input": TEXT, "voice": "af_heart", "response_format": "wav"},
        timeout=300,
    )
    r.raise_for_status()
    assert r.content[:4] == b"RIFF"

    aligned = httpx.post(
        f"{base}/v1/align",
        data={"text": TEXT},
        files={"file": ("take.wav", r.content)},
        timeout=300,
    )
    if aligned.status_code == 404:
        return  # Kokoro gives word times with its speech instead (`/dev/captioned_speech`)
    aligned.raise_for_status()
    words = aligned.json()["timestamps"]
    assert [w["word"] for w in words] == TEXT.split()
    starts = [w["start_time"] for w in words]
    assert starts == sorted(starts) and words[-1]["end_time"] > 0


@pytest.mark.parametrize("base", ENDPOINTS.split(","))
def test_a_tag_the_engine_advertises_changes_the_take_not_the_words(base: str):
    _up(base)
    if "whispering" not in httpx.get(f"{base}/health", timeout=2).json().get("tags", []):
        pytest.skip(f"{base} advertises no whispering tag")
    takes = []
    for text in (TEXT, f"[whispering] {TEXT}"):
        r = httpx.post(
            f"{base}/v1/audio/speech",
            json={"model": "tts-1", "input": text, "response_format": "wav", "seed": 7},
            timeout=300,
        )
        r.raise_for_status()
        takes.append(r.content)
    assert takes[0] != takes[1]
    aligned = httpx.post(
        f"{base}/v1/align", data={"text": TEXT}, files={"file": ("take.wav", takes[1])}, timeout=300
    )
    aligned.raise_for_status()
    assert [w["word"] for w in aligned.json()["timestamps"]] == TEXT.split()
