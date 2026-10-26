"""Gates over HTTP: the inbox, decisions of every kind, send-backs, pause, resume and abort,
with real DBOS on SQLite behind the app, a fake connector and a fake notifier."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from api_fakes import FakeNotifier, until
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_api.main import create_app

WEEKLY = """name: Weekly short
stages:
  - connector: research
    calls: remotion_render
  - gate: pick
    ask: Pick one idea.
    kind: pick
  - connector: script
    calls: remotion_render
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
  - gate: publish_ok
    ask: Publish Tuesday 9am?
    kind: confirm
  - connector: publish
    calls: youtube
"""
QUICK = "name: Quick\nstages:\n  - gate: ok\n    ask: Go?\n    kind: confirm\n"


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def running(tmp_path: Path, calls: list[str], notifier: FakeNotifier) -> Iterator[TestClient]:
    connector = lambda _run, _id, stage: calls.append(stage.name)  # noqa: E731
    app = create_app(tmp_path, connector=connector, notifier=notifier)
    with TestClient(app) as client:
        yield client


def start(client: TestClient, yaml: str = WEEKLY) -> tuple[str, dict]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    wf = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()["workflow_id"]
    return world, client.post(f"/api/workflows/{wf}/runs", json={}).json()


def waiting_at(client: TestClient, run_id: str, stage: str) -> dict:
    def check() -> bool:
        run = client.get(f"/api/runs/{run_id}").json()
        return run["status"] == "waiting_gate" and any(
            s["name"] == stage and s["status"] == "waiting" for s in run["stages"]
        )

    until(check)
    return client.get(f"/api/runs/{run_id}").json()


def test_the_inbox_lists_open_gates_newest_first_and_each_kind_takes_its_decision(
    running: TestClient, calls: list[str], tmp_path: Path
) -> None:
    world, first = start(running)
    waiting_at(running, first["id"], "pick")
    _, second = start(running)
    waiting_at(running, second["id"], "pick")

    gates = running.get("/api/gates").json()
    assert [g["run_id"] for g in gates] == [second["id"], first["id"]]
    gate = gates[1]
    assert gate["world_id"] == world and gate["run_name"] == "Weekly short"
    assert (gate["stage_index"], gate["stage_name"], gate["kind"]) == (1, "pick", "pick")
    assert gate["ask"] == "Pick one idea." and gate["send_backs_left"] == 2
    assert gate["earlier_stages"] == ["research"] and gate["opened_at"]

    url = f"/api/runs/{first['id']}/decision"
    assert running.post(url, json={"note": "no pick"}).status_code == 422
    picked = running.post(url, json={"choice": "idea 2", "note": "Less text on screen."})
    assert picked.status_code == 200
    (decision,) = picked.json()["decisions"]
    assert (decision["kind"], decision["choice"], decision["note"]) == (
        "pick",
        "idea 2",
        "Less text on screen.",
    )
    path = Path(decision["path"])
    assert path.is_relative_to(tmp_path / "worlds" / world / "runs" / first["id"])
    assert path.name == "decision.md" and path.parent.name == "02-pick"
    assert 'choice: "idea 2"' in path.read_text()

    waiting_at(running, first["id"], "review")
    assert running.post(url, json={}).status_code == 200
    waiting_at(running, first["id"], "publish_ok")
    refused = running.post(url, json={"send_back_to": "script"})
    assert refused.status_code == 409
    assert "confirm gate cannot send back" in refused.json()["detail"]
    assert running.post(url, json={"note": "Go."}).status_code == 200
    until(lambda: running.get(f"/api/runs/{first['id']}").json()["status"] == "done")
    done = running.get(f"/api/runs/{first['id']}").json()
    assert [d["kind"] for d in done["decisions"]] == ["pick", "approve", "confirm"]
    assert calls == ["research", "research", "script", "publish"]
    assert [g["run_id"] for g in running.get("/api/gates").json()] == [second["id"]]


def test_send_back_returns_to_the_named_stage_with_the_note_and_the_third_is_refused(
    running: TestClient, calls: list[str]
) -> None:
    _, run = start(running)
    url = f"/api/runs/{run['id']}"
    waiting_at(running, run["id"], "pick")
    running.post(f"{url}/decision", json={"choice": "idea 1"})
    waiting_at(running, run["id"], "review")

    bad = running.post(f"{url}/decision", json={"send_back_to": "publish"})
    assert bad.status_code == 409 and "no earlier stage named 'publish'" in bad.json()["detail"]
    for n, note in ((1, "Too much text."), (2, "Shorter reveal.")):
        sent = running.post(f"{url}/decision", json={"note": note, "send_back_to": "script"})
        assert sent.status_code == 200
        until(lambda n=n: running.get(url).json()["send_backs"] == n)
        run = waiting_at(running, run["id"], "review")
        assert run["stages"][2]["notes"][-1] == note
        assert running.get("/api/gates").json()[0]["send_backs_left"] == 2 - n
    third = running.post(f"{url}/decision", json={"note": "Again.", "send_back_to": "script"})
    assert third.status_code == 409 and "already sent back 2 times" in third.json()["detail"]
    assert calls == ["research", "script", "script", "script"]
    assert len(running.get(url).json()["decisions"]) == 3


def test_pause_resume_and_abort(running: TestClient, calls: list[str]) -> None:
    _, run = start(running)
    url = f"/api/runs/{run['id']}"
    waiting_at(running, run["id"], "pick")
    assert running.post(f"{url}/resume").status_code == 409
    assert running.post(f"{url}/pause").status_code == 200
    until(lambda: running.get(url).json()["status"] == "paused")
    assert running.post(f"{url}/pause").status_code == 409
    assert running.get("/api/gates").json()[0]["run_id"] == run["id"], "a paused gate still shows"

    assert running.post(f"{url}/decision", json={"choice": "idea 1"}).status_code == 200
    until(lambda: running.get(url).json()["stages"][1]["status"] == "done")
    assert running.get(url).json()["status"] == "paused" and calls == ["research"]
    assert running.post(f"{url}/resume").status_code == 200
    waiting_at(running, run["id"], "review")
    assert calls == ["research", "script"]

    assert running.post(f"{url}/abort").status_code == 200
    until(lambda: running.get(url).json()["status"] == "aborted")
    aborted = running.get(url).json()
    statuses = [s["status"] for s in aborted["stages"]]
    assert statuses == ["done", "done", "done", "failed", "pending", "pending"]
    for action in ("pause", "resume", "abort", "decision"):
        response = running.post(f"{url}/{action}", json={})
        assert response.status_code == 409 and "run is aborted" in response.json()["detail"]
    for action in ("pause", "resume", "abort", "decision"):
        assert running.post(f"/api/runs/run_nope/{action}", json={}).status_code == 404


def test_gate_opened_and_run_ended_reach_the_notifier_and_the_world_stream(
    running: TestClient, notifier: FakeNotifier
) -> None:
    world, run = start(running, QUICK)
    waiting_at(running, run["id"], "ok")
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    until(lambda: len(notifier.events) == 2)
    opened, ended = notifier.events
    assert opened.type == "gate.opened" and opened.payload["ask"] == "Go?"
    assert opened.payload["run_id"] == run["id"] and opened.world_id == world
    assert ended.type == "run.ended" and ended.payload["status"] == "done"


def test_gate_and_end_events_stream_to_the_world(live_url: str) -> None:
    world = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()["id"]
    wf = httpx.post(f"{live_url}/api/worlds/{world}/workflows", json={"yaml": QUICK}).json()
    with httpx.stream("GET", f"{live_url}/api/worlds/{world}/events", timeout=10) as r:
        events = sse_events(r)
        run = httpx.post(f"{live_url}/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        seen: list[str] = []
        for kind, _, data in events:
            if kind == "gate.opened":
                assert data["payload"]["stage"] == "ok" and data["run_id"] == run["id"]
                httpx.post(f"{live_url}/api/runs/{run['id']}/decision", json={})
            if kind in ("gate.opened", "run.ended"):
                seen.append(kind)
            if kind == "run.ended":
                assert data["payload"]["status"] == "done"
                break
    assert seen == ["gate.opened", "run.ended"]
