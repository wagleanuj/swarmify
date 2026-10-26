"""The whiteboard over HTTP: real DBOS on SQLite behind the app, a run waiting at a gate in front
of its swarm stage, and an injectable clock so a silent claim can time out without waiting."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from api_fakes import until
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_api.main import create_app
from swarmify_swarm import SqliteStorage

GATE_THEN_SWARM = """name: Brief then research
stages:
  - gate: brief
    ask: Go?
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    tools: [web_search]
"""
T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def running(tmp_path: Path, clock: Clock) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path, clock=clock, sweep_seconds=0.05)) as client:
        yield client


def _swarm_stage(client: httpx.Client | TestClient) -> tuple[str, dict, str]:
    """(world id, the run once it waits at its gate, the tasks url of its swarm stage)."""
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    wf = client.post(f"/api/worlds/{world}/workflows", json={"yaml": GATE_THEN_SWARM}).json()
    run = client.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    run = client.get(f"/api/runs/{run['id']}").json()
    return world, run, f"/api/runs/{run['id']}/stages/{run['stages'][1]['id']}/tasks"


def test_add_a_task_by_hand_to_a_swarm_stage(running: TestClient) -> None:
    _, run, url = _swarm_stage(running)
    assert running.get(url).json() == []
    added = running.post(url, json={"title": " Check @datingdecoded this week "})
    assert added.status_code == 201
    task = added.json()
    assert (task["title"], task["status"], task["owner"], task["added_by"]) == (
        "Check @datingdecoded this week",
        "open",
        None,
        "operator",
    )
    assert (task["run_id"], task["stage_id"], task["world_id"]) == (
        run["id"],
        run["stages"][1]["id"],
        run["world_id"],
    )
    after = running.post(url, json={"title": "Rank", "depends_on": [task["id"]]}).json()
    assert after["depends_on"] == [task["id"]]
    assert [t["title"] for t in running.get(url).json()] == [
        "Check @datingdecoded this week",
        "Rank",
    ]
    assert running.get(f"/api/tasks/{task['id']}").json() == task


def test_only_a_swarm_stage_of_a_known_run_takes_tasks(running: TestClient) -> None:
    _, run, url = _swarm_stage(running)
    gate_url = f"/api/runs/{run['id']}/stages/{run['stages'][0]['id']}/tasks"
    refused = running.post(gate_url, json={"title": "x"})
    assert refused.status_code == 409 and "only swarm stages" in refused.json()["detail"]
    assert running.post(url, json={"title": "   "}).status_code == 422
    assert running.post(url, json={"title": "x", "depends_on": ["task_nope"]}).status_code == 409
    nope = f"/api/runs/{run['id']}/stages/stage_nope/tasks"
    assert running.post(nope, json={"title": "x"}).status_code == 404
    assert running.get(nope).status_code == 404
    assert running.get("/api/runs/run_nope/stages/stage_nope/tasks").status_code == 404
    assert running.get("/api/tasks/task_nope").status_code == 404
    assert running.post("/api/tasks/task_nope/claim", json={"agent": "a"}).status_code == 404


def test_claim_heartbeat_finish_over_http(running: TestClient, clock: Clock) -> None:
    _, _, url = _swarm_stage(running)
    task = running.post(url, json={"title": "Pull top shorts"}).json()
    act = f"/api/tasks/{task['id']}"
    claimed = running.post(f"{act}/claim", json={"agent": "gilfoyle"})
    assert claimed.status_code == 200
    assert claimed.json()["owner"] == "gilfoyle"
    assert claimed.json()["heartbeat_at"] == T0.isoformat()
    lost = running.post(f"{act}/claim", json={"agent": "dinesh"})
    assert lost.status_code == 409 and lost.json()["detail"] == "claimed by gilfoyle"

    clock.now = T0 + timedelta(seconds=60)
    beat = running.post(f"{act}/heartbeat", json={"agent": "gilfoyle", "note": "6 found."}).json()
    assert beat["heartbeat_at"] == clock.now.isoformat()
    assert running.post(f"{act}/heartbeat", json={"agent": "dinesh"}).status_code == 409
    done = running.post(f"{act}/finish", json={"agent": "gilfoyle", "note": "12 ranked."}).json()
    assert done["status"] == "done"
    assert [n["body"] for n in done["notes"]] == ["6 found.", "12 ranked."]


def test_a_silent_claim_returns_to_open_with_its_partial_notes(
    running: TestClient, clock: Clock
) -> None:
    _, _, url = _swarm_stage(running)
    task = running.post(url, json={"title": "Pull top shorts"}).json()
    act = f"/api/tasks/{task['id']}"
    running.post(f"{act}/claim", json={"agent": "gilfoyle"})
    running.post(f"{act}/heartbeat", json={"agent": "gilfoyle", "note": "Halfway."})
    clock.now = T0 + timedelta(minutes=1, seconds=59)
    time.sleep(0.2)
    assert running.get(act).json()["status"] == "claimed"
    clock.now = T0 + timedelta(minutes=2, seconds=1)
    until(lambda: running.get(act).json()["status"] == "open")
    reopened = running.get(act).json()
    assert reopened["owner"] is None
    assert [n["author"] for n in reopened["notes"]] == ["gilfoyle", "swarmify"]
    assert reopened["notes"][0]["body"] == "Halfway."
    assert running.post(f"{act}/claim", json={"agent": "jared"}).json()["owner"] == "jared"


def test_one_failed_sweep_does_not_stop_the_sweeper(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, calls = SqliteStorage.expire, []

    def flaky(self: SqliteStorage, now: datetime | None = None) -> list:
        calls.append(now)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, now)

    monkeypatch.setattr(SqliteStorage, "expire", flaky)
    with TestClient(create_app(tmp_path, clock=clock, sweep_seconds=0.05)) as client:
        until(lambda: len(calls) >= 1)
        _, _, url = _swarm_stage(client)
        task = client.post(url, json={"title": "Pull top shorts"}).json()
        act = f"/api/tasks/{task['id']}"
        client.post(f"{act}/claim", json={"agent": "gilfoyle"})
        clock.now = T0 + timedelta(minutes=2, seconds=1)
        until(lambda: client.get(act).json()["status"] == "open")


def test_deleting_the_world_takes_its_tasks(running: TestClient) -> None:
    world, _, url = _swarm_stage(running)
    task = running.post(url, json={"title": "x"}).json()
    assert running.delete(f"/api/worlds/{world}").status_code == 204
    assert running.get(f"/api/tasks/{task['id']}").status_code == 404


def test_parallel_http_claims_have_one_winner_and_stream_live(live_url: str) -> None:
    with httpx.Client(base_url=live_url, timeout=10) as client:
        world, _, url = _swarm_stage(client)
        task = client.post(url, json={"title": "Contended"}).json()
    agents = 8
    barrier = threading.Barrier(agents)
    codes: list[int] = [0] * agents

    def claim(i: int) -> None:
        with httpx.Client(base_url=live_url, timeout=10) as own:
            barrier.wait()
            codes[i] = own.post(
                f"/api/tasks/{task['id']}/claim", json={"agent": f"a{i}"}
            ).status_code

    with httpx.stream("GET", f"{live_url}/api/worlds/{world}/events", timeout=10) as r:
        events = sse_events(r)
        threads = [threading.Thread(target=claim, args=(i,)) for i in range(agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes) == [200] + [409] * (agents - 1)
        seen: list[tuple[str, str | None]] = []
        for kind, _, data in events:
            if kind == "task.updated" and data["payload"]["id"] == task["id"]:
                seen.append((data["payload"]["status"], data["payload"]["owner"]))
                if seen[-1][0] == "claimed":
                    break
    winner = f"a{codes.index(200)}"
    assert seen == [("open", None), ("claimed", winner)]
