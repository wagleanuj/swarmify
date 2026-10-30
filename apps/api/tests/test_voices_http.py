"""The Assets page's API: a world's voice catalog listed with clip addresses and the workflows
that use each voice, voices added with a validated clip, edited and deleted, and files under
assets/ renamed and deleted, never outside it and never the catalog by hand."""

import io
import json
import math
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from swarmify_connectors import speech

SPOKEN = """name: Bedtime
stages:
  - swarm: script
    goal: Write a script.
    done_when: A script.
    output_type: script
    produces: [script.md]
  - connector: voice
    calls: speech
    input: script.md
    output: timings.json
    params: {voice: me}
"""


def wav(seconds: float, rate: int = 8000) -> bytes:
    frames = b"".join(
        int(9000 * math.sin(i / 3)).to_bytes(2, "little", signed=True)
        for i in range(int(seconds * rate))
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def add(client: TestClient, world: str, name: str, clip: bytes, consent: str = "Mine."):  # noqa: ANN201
    form = {"name": name, "description": "My voice", "tags": "warm, calm", "consent": consent}
    return client.post(
        f"/api/worlds/{world}/voices", data=form, files={"clip": ("me.wav", clip, "audio/wav")}
    )


def test_a_voice_is_added_heard_used_edited_and_deleted(client: TestClient, tmp_path: Path) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    url = f"/api/worlds/{world}/voices"
    assert client.get(url).json() == {"voices": [], "warnings": []}
    flow = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SPOKEN})
    assert flow.status_code == 201, flow.json()
    added = add(client, world, "me", wav(6))
    assert added.status_code == 201, added.json()
    me = {
        "name": "me", "description": "My voice", "tags": ["warm", "calm"],
        "engine": "chatterbox", "consent": "Mine.", "preset": None,
        "clip": "assets/voices/me.wav", "seconds": 6.0,
        "used_by": [{"id": flow.json()["workflow_id"], "name": "Bedtime"}],
    }  # fmt: skip
    assert added.json() == me and client.get(url).json() == {"voices": [me], "warnings": []}
    assert client.get(f"/api/worlds/{world}/files/assets/voices/me.wav").content == wav(6)
    # the speech connector reads the same catalog unchanged
    assets = tmp_path / "worlds" / world / "assets"
    found, warnings = speech.catalog(str(assets))
    assert [v["name"] for v in found] == ["me"] and warnings == []

    edited = client.patch(f"{url}/me", json={"description": "Warmer", "tags": ["bedtime"]})
    assert edited.json() == {**me, "description": "Warmer", "tags": ["bedtime"]}
    assert client.patch(f"{url}/nope", json={"description": "x"}).status_code == 404
    assert client.patch(f"{url}/me", json={"description": " "}).status_code == 422

    assert client.delete(f"{url}/me").status_code == 204
    assert client.get(url).json()["voices"] == [] and not (assets / "voices/me.wav").exists()
    assert json.loads((assets / "voices/voices.json").read_text()) == []
    assert client.delete(f"{url}/me").status_code == 404
    assert client.get("/api/worlds/world_nope/voices").status_code == 404


def test_a_voice_is_refused_like_the_speech_connector_refuses_it(client: TestClient) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    for name, clip, consent, says in [
        ("me", wav(3), "Mine.", "3.0 s; Chatterbox clones from 5 to 10 s"),
        ("me", wav(12), "Mine.", "12.0 s"),
        ("me", b"ID3 an mp3", "Mine.", "16-bit WAV"),
        ("me", wav(6), " ", "consent"),
        ("my voice", wav(6), "Mine.", "letters, digits"),
    ]:
        refused = add(client, world, name, clip, consent)
        assert refused.status_code == 422 and says in refused.json()["detail"], refused.json()
    assert add(client, world, "me", wav(6)).status_code == 201
    taken = add(client, world, "me", wav(7))
    assert taken.status_code == 422 and "already named me" in taken.json()["detail"]


def test_files_are_renamed_and_deleted_inside_assets_never_the_voices(
    client: TestClient,
) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    url = f"/api/worlds/{world}/assets"
    client.put(f"{url}/bed.mp3", content=b"ID3")
    client.put(f"{url}/logo.png", content=b"PNG")
    assert add(client, world, "me", wav(6)).status_code == 201
    renamed = client.patch(f"{url}/assets/bed.mp3", json={"name": "bed-calm.mp3"})
    assert renamed.json() == {"path": "assets/bed-calm.mp3", "name": "bed-calm.mp3", "bytes": 3}
    taken = client.patch(f"{url}/assets/logo.png", json={"name": "bed-calm.mp3"})
    assert taken.status_code == 422 and "already there" in taken.json()["detail"]
    assert client.patch(f"{url}/assets/logo.png", json={"name": "../x"}).status_code == 422
    for path in ("assets/voices/voices.json", "assets/voices/me.wav"):
        assert client.delete(f"{url}/{path}").status_code == 422
        assert client.patch(f"{url}/{path}", json={"name": "x.json"}).status_code == 422
    for path in ("assets/nope.png", "assets/../worlds.db", "runs/x.md"):
        assert client.delete(f"{url}/{path}").status_code == 404
    assert (
        client.delete(f"{url}/assets%2Flogo.png").status_code == 204
    )  # as the web client sends it
    assert [a["path"] for a in client.get(url).json()] == [
        "assets/bed-calm.mp3",
        "assets/voices/me.wav",
        "assets/voices/voices.json",
    ]
