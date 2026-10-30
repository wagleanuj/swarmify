"""Every way a run of a workflow version starts runs today's workflow check first (#102): a
version that breaks a rule now (here: its connector was removed after it was saved) is
refused at start, promote, fork, a child run and over MCP, and the world's list marks it."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from swarmify_api.main import run_control, task_board, workflow_service, world_service
from swarmify_api.mcp import server

RENDER = {
    "name": "render_video",
    "kind": "stage",
    "server": "uvx render-mcp",
    "stage_types": ["connector"],
}
SHORT = """name: Render then pick
stages:
  - connector: render
    calls: render_video
  - gate: pick
    ask: Ship it?
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def setup(client: TestClient) -> tuple[str, str, str]:
    """A world, its render connector and a workflow that passes today's check while it lasts."""
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    client.post(f"/api/worlds/{world}/connectors", json=RENDER).raise_for_status()
    connector = f"/api/worlds/{world}/packages/render_video"  # disabling it removes it
    created = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SHORT})
    assert created.status_code == 201, created.json()
    return world, connector, created.json()["workflow_id"]


def stale(client: TestClient, connector: str) -> None:
    """Remove the connector the workflow calls, so its saved version now fails the check."""
    assert client.delete(connector).status_code == 204


def refused(response, world: str, client: TestClient, runs_before: int) -> None:  # noqa: ANN001
    assert response.status_code == 409, response.json()
    body = response.json()
    assert "render_video" in body["detail"]
    assert body["violations"] and all("message" in v for v in body["violations"])
    assert len(client.get(f"/api/worlds/{world}/runs").json()) == runs_before


def test_start_refuses_a_version_that_fails_todays_check(running: TestClient) -> None:
    world, connector, workflow = setup(running)
    stale(running, connector)
    refused(running.post(f"/api/workflows/{workflow}/runs", json={}), world, running, 0)
    pinned = running.post(f"/api/workflows/{workflow}/runs", json={"version": 1})
    refused(pinned, world, running, 0)


def test_promote_refuses_a_version_that_fails_todays_check(
    running: TestClient, tmp_path: Path
) -> None:
    world, connector, workflow = setup(running)
    artifact = tmp_path / "worlds" / world / "runs" / "run_x" / "s" / "artifacts" / "cut.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("A cut.")
    stale(running, connector)
    body = {"artifact": "runs/run_x/s/artifacts/cut.md", "stage": 1}
    refused(running.post(f"/api/workflows/{workflow}/promote", json=body), world, running, 0)


def test_fork_refuses_a_version_that_fails_todays_check(running: TestClient) -> None:
    world, connector, workflow = setup(running)
    run = running.post(f"/api/workflows/{workflow}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    stale(running, connector)
    refused(running.post(f"/api/runs/{run['id']}/fork", json={"stage": 1}), world, running, 1)


def test_a_child_run_of_a_version_that_fails_the_check_fails_its_stage(
    running: TestClient,
) -> None:
    world, connector, child = setup(running)
    parent_yaml = f"name: Parent\nstages:\n  - workflow: sub\n    runs: {child}\n"
    parent = running.post(f"/api/worlds/{world}/workflows", json={"yaml": parent_yaml}).json()
    stale(running, connector)
    run = running.post(f"/api/workflows/{parent['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "failed")
    stage = running.get(f"/api/runs/{run['id']}").json()["stages"][0]
    assert stage["child"] is None and "render_video" in (stage["error"] or "")
    assert len(running.get(f"/api/worlds/{world}/runs").json()) == 1


def test_the_world_list_marks_workflows_whose_current_version_fails(running: TestClient) -> None:
    world, connector, workflow = setup(running)
    (listed,) = running.get(f"/api/worlds/{world}/workflows").json()
    assert listed["id"] == workflow and listed["violations"] == []
    stale(running, connector)
    (listed,) = running.get(f"/api/worlds/{world}/workflows").json()
    assert any("render_video" in v["message"] for v in listed["violations"])


@pytest.mark.anyio
async def test_mcp_start_and_fork_refuse_with_the_violations(tmp_path: Path) -> None:
    from swarmify_api.main import create_app

    control = run_control(tmp_path)
    with TestClient(create_app(tmp_path, connector=lambda *_: None, retro=False)) as http:
        world, connector, workflow = setup(http)
        run = http.post(f"/api/workflows/{workflow}/runs", json={}).json()
        until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
        stale(http, connector)
        mcp = server(
            world_service(tmp_path), workflow_service(tmp_path), task_board(tmp_path), control
        )
        async with Client(mcp) as client:
            for tool, args in (
                ("start_run", {"workflow_id": workflow}),
                ("fork_run", {"run_id": run["id"], "stage": 1}),
            ):
                result = await client.call_tool(tool, args)
                assert not result.is_error and result.structured_content, tool
                violations = result.structured_content["violations"]
                assert any("render_video" in v["message"] for v in violations), tool
        assert len(http.get(f"/api/worlds/{world}/runs").json()) == 1
    control.close()
