"""Connectors over HTTP: the registry, test-and-list-tools, and a connector stage's raw call,
with the fake ConnectorTransport and real DBOS on SQLite behind the app."""

import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_connectors import ConnectorError, FakeTransport, Tool

FAL = {
    "name": "fal_video",
    "kind": "stage",
    "server": "uvx fal-mcp",
    "credentials": {"FAL_KEY": "s3cret"},
    "stage_types": ["connector"],
}
CLIP = """name: Clip then pick
stages:
  - connector: clip
    calls: fal_video
    output: clip.mp4
    params: {seconds: 5}
  - gate: pick
    ask: Keep it?
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def world(client: TestClient) -> str:
    return client.post("/api/worlds", json={"name": "Video"}).json()["id"]


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_new_world_has_web_search_and_the_graph_with_no_setup(client: TestClient) -> None:
    listed = client.get(f"/api/worlds/{world(client)}/connectors").json()
    ready = {c["name"]: c for c in listed if c["kind"] == "tool"}
    assert set(ready) == {"web_search", "graph"}
    assert all(c["builtin"] and c["missing"] == [] for c in ready.values())
    assert all(c["stage_types"] == ["swarm"] for c in ready.values())
    assert client.get("/api/worlds/world_nope/connectors").status_code == 404


def test_test_lists_the_tools_of_a_draft_without_saving_it(
    client: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    w = world(client)
    transport.tools["fal_video"] = [Tool("fal_video", "Make a clip.", {"type": "object"})]
    tested = client.post(f"/api/worlds/{w}/connectors/test", json=FAL)
    assert tested.status_code == 200
    assert tested.json() == {
        "tools": [
            {"name": "fal_video", "description": "Make a clip.", "input_schema": {"type": "object"}}
        ]
    }
    assert transport.starts == [("fal_video", {"FAL_KEY": "s3cret"})]
    assert "fal_video" not in [c["name"] for c in client.get(f"/api/worlds/{w}/connectors").json()]
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"name": "Fal Video"}, "lower_snake_case"),
        ({"credentials": {"FAL_KEY": ""}}, "needs FAL_KEY"),
        ({"name": "nobody_home"}, "could not start"),
    ],
)
def test_test_explains_what_went_wrong(
    client: TestClient, over: dict[str, Any], problem: str
) -> None:
    answer = client.post(f"/api/worlds/{world(client)}/connectors/test", json={**FAL, **over})
    assert answer.status_code == 422 and problem in answer.json()["detail"]


def test_add_keeps_the_secret_in_the_home_file_and_never_returns_it(
    client: TestClient, tmp_path: Path
) -> None:
    w = world(client)
    added = client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert added.status_code == 201
    body = added.json()
    assert (body["name"], body["command"], body["credentials"]) == (
        "fal_video",
        ["uvx", "fal-mcp"],
        ["FAL_KEY"],
    )
    assert body["missing"] == [] and "s3cret" not in added.text
    env = tmp_path / ".env"
    assert env.read_text() == "FAL_KEY=s3cret\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    listed = client.get(f"/api/worlds/{w}/connectors")
    assert "s3cret" not in listed.text and listed.json()[-1]["id"] == body["id"]

    again = client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert again.status_code == 422 and "already exists" in again.json()["detail"]
    assert client.post("/api/worlds/world_nope/connectors", json=FAL).status_code == 404


def test_workflows_see_the_registry(client: TestClient) -> None:
    w = world(client)
    swarm = (
        "name: s\nstages:\n  - swarm: r\n    goal: g\n    done_when: d\n    tools: [fal_video]\n"
    )
    assert client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).status_code == 422
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).status_code == 201
    refused = client.post(f"/api/worlds/{w}/workflows", json={"yaml": swarm})
    assert [v["rule"] for v in refused.json()["violations"]] == ["connector_stage_type"]


def test_delete_removes_an_added_connector_but_never_a_built_in(client: TestClient) -> None:
    w = world(client)
    added = client.post(f"/api/worlds/{w}/connectors", json=FAL).json()
    assert client.delete("/api/connectors/builtin_web_search").status_code == 409
    assert client.delete(f"/api/connectors/{added['id']}").status_code == 204
    assert client.delete(f"/api/connectors/{added['id']}").status_code == 404
    assert client.get(f"/api/worlds/{w}/connectors").json()[-1]["builtin"]


def start(client: TestClient) -> dict[str, Any]:
    w = world(client)
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    workflow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).json()
    return client.post(f"/api/workflows/{workflow['workflow_id']}/runs", json={}).json()


def test_a_connector_stage_records_its_raw_call_keyed_by_the_stage_id(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "fal_status"}]
    transport.outputs["fal_status"] = [
        {"status": "running", "progress": 0.5},
        {"status": "done", "url": "https://cdn/clip.mp4"},
    ]
    run = start(running)
    url = f"/api/runs/{run['id']}"
    until(lambda: running.get(url).json()["status"] == "waiting_gate")
    stage_id = running.get(url).json()["stages"][0]["id"]

    call = running.get(f"/api/stages/{stage_id}/call").json()
    files = f"worlds/{run['world_id']}/runs/{run['id']}/{stage_id}/artifacts"
    assert call["request"] == {
        "seconds": 5,
        "idempotency_key": stage_id,
        "output": "clip.mp4",
        "output_path": str(tmp_path / files / "clip.mp4"),
        "frontmatter": {"world": run["world_id"], "run": run["id"], "stage": stage_id},
        "assets_path": str(tmp_path / f"worlds/{run['world_id']}/assets"),
    }
    assert call["response"] == {"status": "done", "url": "https://cdn/clip.mp4"}
    assert (call["status"], call["polls"], call["tool"]) == ("done", 2, "fal_video")
    assert (call["run_id"], call["world_id"]) == (run["id"], run["world_id"])
    assert transport.calls[0][3] == {"FAL_KEY": "s3cret"}
    assert running.get(f"/api/stages/{run['stages'][1]['id']}/call").status_code == 404


def test_a_failing_connector_fails_the_run_with_its_error(
    running: TestClient, transport: FakeTransport
) -> None:
    transport.outputs["fal_video"] = [ConnectorError("fal_video: quota exceeded")]
    run = start(running)
    url = f"/api/runs/{run['id']}"
    until(lambda: running.get(url).json()["status"] == "failed")
    stage = running.get(url).json()["stages"][0]
    assert stage["error"] == "ConnectorError: fal_video: quota exceeded"
    call = running.get(f"/api/stages/{stage['id']}/call").json()
    assert (call["status"], call["error"]) == ("failed", "fal_video: quota exceeded")


def test_progress_streams_on_the_world_events(live_url: str, transport: FakeTransport) -> None:
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "fal_status"}]
    transport.outputs["fal_status"] = [
        {"status": "running", "progress": 0.5, "message": "frames"},
        {"status": "done"},
    ]
    w = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()["id"]
    httpx.post(f"{live_url}/api/worlds/{w}/connectors", json=FAL).raise_for_status()
    workflow = httpx.post(f"{live_url}/api/worlds/{w}/workflows", json={"yaml": CLIP}).json()
    seen: list[tuple[str, float | None]] = []
    with httpx.stream("GET", f"{live_url}/api/worlds/{w}/events", timeout=10) as r:
        httpx.post(f"{live_url}/api/workflows/{workflow['workflow_id']}/runs", json={})
        for kind, _, data in sse_events(r):
            if kind == "connector_call.updated":
                seen.append((data["payload"]["status"], data["payload"]["progress"]))
                if seen[-1][0] == "done":
                    break
    assert seen == [("running", None), ("polling", 0.0), ("polling", 0.5), ("done", 1.0)]
