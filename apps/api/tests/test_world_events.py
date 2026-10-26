"""The per-world SSE stream, driven through a real server because the stream never ends."""

import json
from collections.abc import Iterator

import httpx
from fastapi.testclient import TestClient


def _events(response: httpx.Response) -> Iterator[tuple[str, int, dict]]:
    """Yield (type, id, data) per SSE message, skipping keepalive comments."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if fields:
                yield fields["event"], int(fields["id"]), json.loads(fields["data"])
                fields = {}
            continue
        key, _, value = line.partition(": ")
        fields[key] = value


def test_creating_and_deleting_a_world_appear_on_its_stream(live_url: str) -> None:
    world = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()
    with httpx.stream("GET", f"{live_url}/api/worlds/{world['id']}/events", timeout=5) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _events(r)
        kind, seq, data = next(events)
        assert kind == "world.created" and seq == 1
        assert data["payload"] == {"id": world["id"], "name": "Video", "purpose": ""}
        assert data["world_id"] == world["id"] and data["run_id"] is None

        assert httpx.delete(f"{live_url}/api/worlds/{world['id']}").status_code == 204
        kind, seq, data = next(events)
        assert kind == "world.deleted" and seq == 2
        assert data["payload"] == {"id": world["id"]}


def test_last_event_id_resumes_after_the_given_sequence(live_url: str) -> None:
    a = httpx.post(f"{live_url}/api/worlds", json={"name": "A"}).json()
    b = httpx.post(f"{live_url}/api/worlds", json={"name": "B"}).json()
    httpx.delete(f"{live_url}/api/worlds/{b['id']}")
    headers = {"Last-Event-ID": "2"}
    url = f"{live_url}/api/worlds/{b['id']}/events"
    with httpx.stream("GET", url, headers=headers, timeout=5) as r:
        kind, seq, data = next(_events(r))
        assert (kind, seq) == ("world.deleted", 3)
    url = f"{live_url}/api/worlds/{a['id']}/events"
    with httpx.stream("GET", url, timeout=5) as r:
        kind, seq, _ = next(_events(r))
        assert (kind, seq) == ("world.created", 1)


def test_a_bad_last_event_id_is_a_422_and_an_unknown_world_a_404(client: TestClient) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()
    url = f"/api/worlds/{world['id']}/events"
    for bad in ["abc", "", "1.5"]:
        assert client.get(url, headers={"Last-Event-ID": bad}).status_code == 422, bad
    assert client.get("/api/worlds/world_nope/events").status_code == 404
