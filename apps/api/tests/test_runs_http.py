"""Runs over HTTP, with real DBOS on SQLite behind the app and the fake ConnectorTransport."""

from collections.abc import Iterator

import httpx
import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_connectors import FakeTransport
from swarmify_runs import StageDef

RENDER = {
    "name": "render_video",
    "kind": "stage",
    "server": "uvx render-mcp",
    "stage_types": ["connector"],
}
"""A connector the test world adds; the fake transport answers for it."""

SHORT = """name: Render then pick
stages:
  - connector: render
    calls: render_video
  - gate: pick
    ask: Ship it?
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    """A client whose lifespan ran, so DBOS is launched."""
    with TestClient(app) as client:
        yield client


def _setup(client: TestClient) -> tuple[str, str]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=RENDER).status_code == 201
    created = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SHORT})
    return world, created.json()["workflow_id"]


def test_start_a_run_and_watch_it_reach_its_gate(
    running: TestClient, transport: FakeTransport
) -> None:
    world, workflow = _setup(running)
    started = running.post(f"/api/workflows/{workflow}/runs", json={})
    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "queued" and run["workflow_version"] == 1
    assert run["name"] == "Render then pick"
    assert [(s["name"], s["type"], s["status"]) for s in run["stages"]] == [
        ("render", "connector", "pending"),
        ("pick", "gate", "pending"),
    ]

    url = f"/api/runs/{run['id']}"
    until(lambda: running.get(url).json()["status"] == "waiting_gate")
    stages = running.get(url).json()["stages"]
    assert [s["status"] for s in stages] == ["done", "waiting"]
    assert [(c[1], c[2]["idempotency_key"]) for c in transport.calls] == [
        ("render_video", stages[0]["id"])
    ]
    assert [r["id"] for r in running.get(f"/api/worlds/{world}/runs").json()] == [run["id"]]


def test_a_run_pins_the_version_it_started_with(running: TestClient) -> None:
    _, workflow = _setup(running)
    edited = SHORT.replace("Ship it?", "Really ship it?")
    assert (
        running.post(f"/api/workflows/{workflow}/versions", json={"yaml": edited}).status_code
        == 201
    )
    pinned = running.post(f"/api/workflows/{workflow}/runs", json={"version": 1}).json()
    latest = running.post(f"/api/workflows/{workflow}/runs", json={}).json()
    assert (pinned["workflow_version"], latest["workflow_version"]) == (1, 2)


def test_unknown_things_are_404s(running: TestClient) -> None:
    _, workflow = _setup(running)
    assert running.post("/api/workflows/wf_nope/runs", json={}).status_code == 404
    assert running.post(f"/api/workflows/{workflow}/runs", json={"version": 9}).status_code == 404
    assert running.get("/api/runs/run_nope").status_code == 404
    assert running.get("/api/worlds/world_nope/runs").status_code == 404


def test_deleting_the_world_takes_its_runs(running: TestClient) -> None:
    world, workflow = _setup(running)
    run = running.post(f"/api/workflows/{workflow}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    assert running.delete(f"/api/worlds/{world}").status_code == 204
    assert running.get(f"/api/runs/{run['id']}").status_code == 404


def test_run_updates_stream_on_the_world_events(live_url: str) -> None:
    world = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()["id"]
    httpx.post(f"{live_url}/api/worlds/{world}/connectors", json=RENDER).raise_for_status()
    workflow = httpx.post(f"{live_url}/api/worlds/{world}/workflows", json={"yaml": SHORT})
    with httpx.stream("GET", f"{live_url}/api/worlds/{world}/events", timeout=10) as r:
        events = sse_events(r)
        run = httpx.post(f"{live_url}/api/workflows/{workflow.json()['workflow_id']}/runs", json={})
        seen: list[str] = []
        for kind, _, data in events:
            if kind != "run.updated":
                continue  # connector_call.updated events interleave
            assert data["run_id"] == run.json()["id"]
            seen.append(data["payload"]["status"])
            if seen[-1] == "waiting_gate":
                break
    assert seen == ["queued", "running", "waiting_gate"]


def test_stage_def_keeps_every_stage_field() -> None:
    from swarmify_api.runs import stage_def
    from swarmify_workflows import GateStage

    assert stage_def(GateStage("pick", "Pick one.", "pick")) == StageDef(
        "pick", "gate", {"ask": "Pick one.", "kind": "pick"}
    )


ONE_GATE = "name: Quick check\nstages:\n  - gate: ok\n    ask: Fine?\n    kind: confirm\n"


def test_a_done_run_is_saved_as_a_workflow_from_the_stages_it_ran(running: TestClient) -> None:
    world = running.post("/api/worlds", json={"name": "Video"}).json()["id"]
    wf = running.post(f"/api/worlds/{world}/workflows", json={"yaml": ONE_GATE}).json()
    run = running.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    early = running.post(f"/api/runs/{run['id']}/workflow", json={})
    assert early.status_code == 409 and "waiting_gate" in early.json()["detail"]

    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    saved = running.post(f"/api/runs/{run['id']}/workflow", json={"name": "Checked again"})
    assert saved.status_code == 201, saved.text
    version = saved.json()
    assert version["number"] == 1 and version["workflow_id"] != wf["workflow_id"]
    assert version["model"]["name"] == "Checked again"
    assert version["model"]["stages"] == [
        {"type": "gate", "name": "ok", "ask": "Fine?", "kind": "confirm"}
    ]
    assert (
        version["yaml"]
        == "name: Checked again\nstages:\n- gate: ok\n  ask: Fine?\n  kind: confirm\n"
    )
    names = [w["name"] for w in running.get(f"/api/worlds/{world}/workflows").json()]
    assert names == ["Quick check", "Checked again"]
    assert running.post("/api/runs/run_nope/workflow", json={}).status_code == 404
