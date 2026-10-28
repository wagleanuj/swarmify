"""Budget and baseline over HTTP: real DBOS on SQLite, the real Agent SDK client with a scripted
CLI behind its transport pricing each tool call at $1.00, and a recording notifier."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import FakeNotifier, until
from fastapi.testclient import TestClient
from mcp import Client

from swarmify_api.main import create_app, run_control, task_board, workflow_service, world_service
from swarmify_api.mcp import server
from swarmify_memory import FakeGraphIndex
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

RESEARCH = """name: Research
budget_usd: 3.5
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
"""
GRAPH = Call(tool_id("query_graph"), {"question": "What did we reject before?"})
CLAIM = Call(tool_id("claim_task"))
NOTE = Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks in the first second."})
IDEAS = Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"})
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
PASS = [Answer({"passed": True, "reasons": ["Five ideas."]})]
DOLLAR = {"input_tokens": 250_000}
"""A model step's usage that the adapter prices at $1.00."""


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def running(tmp_path: Path, script: Script, notifier: FakeNotifier) -> Iterator[TestClient]:
    app = create_app(
        tmp_path,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=FakeGraphIndex(),
        notifier=notifier,
        extract=lambda *_: None,
        retro=False,
    )
    with TestClient(app) as client:
        yield client


def _workflow(client: TestClient, yaml: str = RESEARCH) -> str:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    return client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()["workflow_id"]


def _run(client: TestClient, run_id: str) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


def test_eighty_percent_warns_and_a_hundred_stops_the_agents_at_a_budget_gate(
    running: TestClient, script: Script, notifier: FakeNotifier
) -> None:
    # Every session's result says $0.25 in total; every tool call's step costs $1.00.
    script.scripts = [PLAN, [GRAPH, CLAIM, NOTE, IDEAS, Say("Done.")], [IDEAS, FINISH], PASS]
    script.usage, script.cost = DOLLAR, 0.25
    wf = _workflow(running)
    run = running.post(f"/api/workflows/{wf}/runs", json={}).json()
    assert run["budget_usd"] == 3.5 and run["spent_usd"] == 0 and run["baseline"] is False

    until(lambda: [e.type for e in notifier.events] == ["budget.warning", "gate.opened"])
    stopped = _run(running, run["id"])
    # planner 0.25, then the worker's steps: 1.25, 2.25, 3.25 (warns), 4.25 (stops)
    assert stopped["status"] == "waiting_gate" and stopped["spent_usd"] == 4.25
    assert stopped["stages"][0]["status"] == "waiting"
    assert notifier.events[0].payload["spent_usd"] == 3.25  # the third call crossed $2.80
    assert notifier.events[1].payload["kind"] == "budget"

    (gate,) = running.get("/api/gates").json()
    assert gate["kind"] == "budget" and gate["budget_usd"] == 3.5 and gate["spent_usd"] == 4.25
    assert "budget of $3.50 is spent" in gate["ask"]
    agents = running.get(f"/api/runs/{run['id']}/agents").json()
    (agent,) = [a for a in agents if a["name"] == "worker-1"]
    assert agent["status"] == "working" and agent["cost_usd"] == 4.0
    stage = stopped["stages"][0]["id"]
    log = running.get(f"/api/runs/{run['id']}/stages/{stage}/log").json()
    assert "Stopped: The run's budget of $3.50 is spent." in log[-1]["detail"]

    decided = running.post(f"/api/runs/{run['id']}/decision", json={"choice": "go"})
    assert decided.status_code == 409 and "budget gate" in decided.json()["detail"]
    low = running.post(f"/api/runs/{run['id']}/budget", json={"budget_usd": 4})
    assert low.status_code == 422 and "spent $4.25" in low.json()["detail"]
    assert running.post(f"/api/runs/{run['id']}/budget", json={"budget_usd": 0}).status_code == 422
    assert running.post("/api/runs/run_nope/budget", json={"budget_usd": 9}).status_code == 404

    raised = running.post(f"/api/runs/{run['id']}/budget", json={"budget_usd": 10})
    assert raised.status_code == 200 and raised.json()["budget_usd"] == 10
    until(lambda: _run(running, run["id"])["status"] == "done")
    done = _run(running, run["id"])
    # the resumed session's two steps, then its result trues it to $0.25; the judge $0.25
    assert done["spent_usd"] == 4.75 and not done["over_budget"]
    _, first, second, _ = script.sessions
    assert first.resume is None and second.resume == agent["session_id"]
    assert running.get("/api/gates").json() == []


def test_a_start_may_override_the_budget(running: TestClient, script: Script) -> None:
    script.scripts = [PLAN, [GRAPH, CLAIM, Say("Done.")]]
    script.usage, script.cost = DOLLAR, 2.0  # the planner alone spends it
    wf = _workflow(running)
    assert running.post(f"/api/workflows/{wf}/runs", json={"budget_usd": -1}).status_code == 422
    run = running.post(f"/api/workflows/{wf}/runs", json={"budget_usd": 1.5}).json()
    assert run["budget_usd"] == 1.5
    until(lambda: _run(running, run["id"])["over_budget"])
    assert _run(running, run["id"])["spent_usd"] == 2.0
    assert len(script.sessions) == 1  # the worker never started
    aborted = running.post(f"/api/runs/{run['id']}/abort")
    assert aborted.status_code == 200
    until(lambda: _run(running, run["id"])["status"] == "aborted")


def test_a_baseline_run_has_one_agent_and_no_graph_and_the_swarm_is_shown_beside_it(
    running: TestClient, script: Script
) -> None:
    script.scripts = [[CLAIM, IDEAS, FINISH, Say("Done.")]]  # no planner, no judge
    script.cost = 0.6
    wf = _workflow(running, RESEARCH.replace("budget_usd: 3.5\n", ""))
    baseline = running.post(f"/api/workflows/{wf}/runs", json={"baseline": True}).json()
    assert baseline["baseline"] is True
    until(lambda: _run(running, baseline["id"])["status"] == "done")
    assert not any(o.is_error for o in script.outcomes)  # claimed with no graph query first
    (options,) = script.sessions
    assert not any("graph" in t for t in options.allowed_tools)

    # sessions are numbered across runs: the baseline's was the first
    script.scripts = [[], PLAN, [GRAPH, CLAIM, FINISH, Say("Done.")], PASS]
    script.cost = 0.4
    swarm = running.post(f"/api/workflows/{wf}/runs", json={}).json()
    assert swarm["baseline"] is False
    until(lambda: _run(running, swarm["id"])["status"] == "done")
    assert tool_id("query_graph") in script.sessions[2].allowed_tools
    assert _run(running, swarm["id"])["spent_usd"] == pytest.approx(1.2)

    measured = running.get(f"/api/runs/{swarm['id']}/baseline").json()
    assert measured["id"] == baseline["id"] and measured["spent_usd"] == 0.6
    assert measured["stages"][0]["started_at"] and measured["stages"][0]["ended_at"]
    other = _workflow(running)
    fresh = running.post(f"/api/workflows/{other}/runs", json={"budget_usd": 50}).json()
    assert running.get(f"/api/runs/{fresh['id']}/baseline").json() is None
    assert running.get("/api/runs/run_nope/baseline").status_code == 404


@pytest.mark.anyio
async def test_the_mcp_server_sets_a_budget(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    # the first step is stopped before its graph query runs, so the resumed session asks again
    script.scripts = [PLAN, [GRAPH, CLAIM, Say("Done.")], [GRAPH, CLAIM, FINISH], PASS]
    script.usage = DOLLAR
    wf = _workflow(running)
    run = running.post(f"/api/workflows/{wf}/runs", json={"budget_usd": 1}).json()
    until(lambda: _run(running, run["id"])["over_budget"])
    control = run_control(tmp_path)
    mcp = server(world_service(tmp_path), workflow_service(tmp_path), task_board(tmp_path), control)
    async with Client(mcp) as client:
        refused = await client.call_tool("set_budget", {"run_id": run["id"], "budget_usd": 1})
        assert refused.is_error
        raised = await client.call_tool("set_budget", {"run_id": run["id"], "budget_usd": 5})
        assert raised.structured_content and raised.structured_content["budget_usd"] == 5
    until(lambda: _run(running, run["id"])["status"] == "done")
    control.close()


def test_an_ad_hoc_run_with_no_budget_typed_gets_the_default_under_the_monthly_cap(
    running: TestClient, script: Script
) -> None:
    script.scripts = [PLAN, [GRAPH, CLAIM, Say("Done.")]]
    world = running.post("/api/worlds", json={"name": "Kids"}).json()["id"]
    run = running.post(f"/api/worlds/{world}/runs", json={"goal": "make good videos"}).json()
    assert run["budget_usd"] == 5.0
    running.post(f"/api/runs/{run['id']}/abort")
    until(lambda: _run(running, run["id"])["status"] == "aborted")
    running.patch(f"/api/worlds/{world}", json={"monthly_budget_usd": 2})
    capped = running.post(f"/api/worlds/{world}/runs", json={"goal": "make good videos"}).json()
    assert capped["budget_usd"] == 2.0
    running.post(f"/api/runs/{capped['id']}/abort")
    until(lambda: _run(running, capped["id"])["status"] == "aborted")
