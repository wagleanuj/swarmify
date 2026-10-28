"""The per-world SSE stream, driven through a real server because the stream never ends."""

import httpx
from fastapi.testclient import TestClient

from sse import sse_events


def test_creating_and_deleting_a_world_appear_on_its_stream(live_url: str) -> None:
    world = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()
    with httpx.stream("GET", f"{live_url}/api/worlds/{world['id']}/events", timeout=5) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = sse_events(r)
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
        kind, seq, data = next(sse_events(r))
        assert (kind, seq) == ("world.deleted", 3)
    url = f"{live_url}/api/worlds/{a['id']}/events"
    with httpx.stream("GET", url, timeout=5) as r:
        kind, seq, _ = next(sse_events(r))
        assert (kind, seq) == ("world.created", 1)


def test_the_all_worlds_stream_carries_every_worlds_events_in_order(live_url: str) -> None:
    a = httpx.post(f"{live_url}/api/worlds", json={"name": "A"}).json()
    b = httpx.post(f"{live_url}/api/worlds", json={"name": "B"}).json()
    with httpx.stream("GET", f"{live_url}/api/events", timeout=5) as r:
        events = sse_events(r)
        seen = [(kind, seq, data["world_id"]) for kind, seq, data in (next(events), next(events))]
        assert seen == [("world.created", 1, a["id"]), ("world.created", 2, b["id"])]
    resume = {"Last-Event-ID": "1"}
    with httpx.stream("GET", f"{live_url}/api/events", headers=resume, timeout=5) as r:
        assert next(sse_events(r))[1] == 2


def test_a_bad_last_event_id_is_a_422_and_an_unknown_world_a_404(client: TestClient) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()
    url = f"/api/worlds/{world['id']}/events"
    for bad in ["abc", "", "1.5"]:
        assert client.get(url, headers={"Last-Event-ID": bad}).status_code == 422, bad
    assert client.get("/api/worlds/world_nope/events").status_code == 404


def test_since_now_skips_history_and_yields_only_later_events(live_url: str) -> None:
    a = httpx.post(f"{live_url}/api/worlds", json={"name": "A"}).json()
    with (
        httpx.stream("GET", f"{live_url}/api/events?since=now", timeout=5) as every,
        httpx.stream("GET", f"{live_url}/api/worlds/{a['id']}/events?since=now", timeout=5) as one,
    ):
        b = httpx.post(f"{live_url}/api/worlds", json={"name": "B"}).json()
        httpx.delete(f"{live_url}/api/worlds/{a['id']}")
        kind, seq, data = next(sse_events(every))
        assert (kind, seq, data["world_id"]) == ("world.created", 2, b["id"])
        kind, seq, data = next(sse_events(one))
        assert (kind, seq, data["world_id"]) == ("world.deleted", 3, a["id"])


def test_last_event_id_wins_over_since_now_so_reconnects_resume(live_url: str) -> None:
    httpx.post(f"{live_url}/api/worlds", json={"name": "A"})
    httpx.post(f"{live_url}/api/worlds", json={"name": "B"})
    resume = {"Last-Event-ID": "1"}
    with httpx.stream("GET", f"{live_url}/api/events?since=now", headers=resume, timeout=5) as r:
        assert next(sse_events(r))[1] == 2


def test_since_must_be_now(client: TestClient) -> None:
    assert client.get("/api/events?since=later").status_code == 422
