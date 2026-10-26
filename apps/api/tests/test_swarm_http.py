"""A swarm stage over HTTP: real DBOS on SQLite, the real Agent SDK client with a scripted CLI
behind its transport, notes and artifacts on disk, and the live log on the world's stream."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_swarm import tool_id
from swarmify_swarm.fake import Call, Die, Say, Script

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    tools: [web_search]
    produces: [ideas.md]
"""
GRAPH = Call(tool_id("query_graph"), {"question": "What did we reject before?"})
CLAIM = Call(tool_id("claim_task"))
NOTE = Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks in the first second."})
IDEAS = Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"})


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 15
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _start(client: httpx.Client | TestClient, tmp_path: Path) -> tuple[str, dict]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    skill = tmp_path / "worlds" / world / "skills" / "hooks" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hooks\ndescription: Write a hook.\n---\n")
    wf = client.post(f"/api/worlds/{world}/workflows", json={"yaml": RESEARCH}).json()
    run = client.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
    return world, run


def _ended(client: httpx.Client | TestClient, run_id: str) -> dict:
    _until(lambda: client.get(f"/api/runs/{run_id}").json()["status"] in ("done", "failed"))
    return client.get(f"/api/runs/{run_id}").json()


def test_a_worker_works_the_stage_and_leaves_notes_artifacts_and_a_log(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [
        [GRAPH, CLAIM, NOTE, IDEAS, Call("Bash", {"command": "ls"}), Say("Five ideas.")]
    ]
    script.cost = 0.042
    world, run = _start(running, tmp_path)
    ended = _ended(running, run["id"])
    assert ended["status"] == "done", ended
    stage = ended["stages"][0]["id"]

    log = running.get(f"/api/runs/{run['id']}/stages/{stage}/log").json()
    allowed = [e["tool"] for e in log if e["kind"] == "tool"]
    assert allowed == [tool_id(n) for n in ("query_graph", "claim_task", "write_note")] + [
        tool_id("write_artifact")
    ]
    assert [e["tool"] for e in log if e["kind"] == "denied"] == ["Bash"]
    assert log[0]["kind"] == "session" and f"{world}:hooks" in log[0]["detail"]
    assert log[-1]["kind"] == "result" and "$0.0420" in log[-1]["detail"]

    (agent,) = running.get(f"/api/runs/{run['id']}/agents").json()
    assert agent["name"] == "worker-1" and agent["status"] == "done"
    assert agent["cost_usd"] == 0.042 and agent["skills"] == [f"{world}:hooks"]
    assert agent["session_id"].startswith("session-")

    folder = tmp_path / "worlds" / world / "runs" / run["id"] / stage
    note = (folder / "notes" / "trends.md").read_text()
    assert 'author: "worker-1"' in note and "Hooks in the first second." in note
    assert (folder / "artifacts" / "ideas.md").read_text().endswith("1. Hooks")
    (task,) = running.get(f"/api/runs/{run['id']}/stages/{stage}/tasks").json()
    assert task["title"] == "Find 5 ideas trending this week." and task["owner"] == "worker-1"

    options = script.sessions[0]
    assert options.setting_sources == [] and options.cwd == folder
    assert options.plugins == [{"type": "local", "path": str(tmp_path / "worlds" / world)}]
    assert "WebSearch" in options.allowed_tools and "Bash" not in options.allowed_tools

    assert running.get(f"/api/runs/{run['id']}/stages/stage_nope/log").status_code == 404
    assert running.get("/api/runs/run_nope/agents").status_code == 404


def test_a_dead_session_is_replaced_by_one_that_resumes_it(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [[GRAPH, Die()], [CLAIM, Say("Resumed.")]]
    _, run = _start(running, tmp_path)
    assert _ended(running, run["id"])["status"] == "done"
    (agent,) = running.get(f"/api/runs/{run['id']}/agents").json()
    first, second = script.sessions
    assert first.resume is None and second.resume == agent["session_id"]


def test_a_stage_whose_sessions_all_die_fails_the_run(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [[Die()]]
    _, run = _start(running, tmp_path)
    ended = _ended(running, run["id"])
    assert ended["status"] == "failed"
    assert "worker-1 failed 2 sessions" in ended["stages"][0]["error"]


def test_the_live_log_streams_on_the_world_events(
    live_url: str, script: Script, tmp_path: Path
) -> None:
    script.scripts = [[GRAPH, CLAIM, NOTE]]
    with httpx.Client(base_url=live_url, timeout=10) as client:
        world, run = _start(client, tmp_path)
        seen: list[str] = []
        with client.stream("GET", f"/api/worlds/{world}/events") as response:
            for type_, _, data in sse_events(response):
                if type_ == "agent.logged" and data["payload"]["kind"] == "tool":
                    seen.append(data["payload"]["tool"])
                if type_ == "artifact.created":
                    assert data["payload"]["type"] == "note"
                    break
        assert seen == [tool_id("query_graph"), tool_id("claim_task"), tool_id("write_note")]
        _ended(client, run["id"])
