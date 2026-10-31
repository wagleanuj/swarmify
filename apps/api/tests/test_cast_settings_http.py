"""World settings, the Cast and Ad Hoc Runs over HTTP: real DBOS on SQLite, the real Agent SDK
client with a scripted CLI behind its transport, the fake GraphIndex and a model-free extract
that rebuilds the graph a tenth of a second after notes land."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dbos import DBOS
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from swarmify_api.main import create_app, world_service
from swarmify_connectors import FakeTransport
from swarmify_memory import FakeGraphIndex
from swarmify_runs import queue_name
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    tools: [web_search]
"""
GRAPH = Call(tool_id("query_graph"), {"question": "What did we reject before?"})
CLAIM = Call(tool_id("claim_task"))
NOTE = Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks in the first second."})
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
WORK = [GRAPH, CLAIM, NOTE, FINISH, Say("Five ideas.")]
PASS = [Answer({"passed": True, "reasons": ["Five ideas."]})]
NOTION = {
    "name": "notion",
    "kind": "tool",
    "server": "npx notion-mcp",
    "stage_types": ["swarm"],
}
"""A tool connector the world adds; an Ad Hoc Run's agents get it."""


@pytest.fixture
def app(
    tmp_path: Path, transport: FakeTransport, script: Script, graph_index: FakeGraphIndex
) -> FastAPI:
    return create_app(
        tmp_path,
        transport=transport,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=graph_index,
        extract=fake_extract,
        graph_debounce=0.1,
        graph_poll=0.02,
        retro=False,
    )


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _until(check: Callable[[], object]) -> None:
    import time

    deadline = time.monotonic() + 15
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _world(client: TestClient) -> str:
    return client.post("/api/worlds", json={"name": "Video"}).json()["id"]


def _run_research(client: TestClient, world: str, yaml: str = RESEARCH) -> dict:
    wf = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
    return client.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()


def _ended(client: TestClient, run_id: str) -> dict:
    _until(lambda: client.get(f"/api/runs/{run_id}").json()["status"] in ("done", "failed"))
    return client.get(f"/api/runs/{run_id}").json()


def _cast(client: TestClient, world: str) -> dict[str, dict]:
    return {m["name"]: m for m in client.get(f"/api/worlds/{world}/cast").json()}


def test_settings_have_defaults_change_one_at_a_time_and_reach_the_run_queue(
    running: TestClient, tmp_path: Path
) -> None:
    world = _world(running)
    shown = running.get(f"/api/worlds/{world}").json()
    assert {
        k: shown[k] for k in ("retention_days", "monthly_budget_usd", "run_cap", "agent_count")
    } == {
        "retention_days": 90,
        "monthly_budget_usd": None,
        "run_cap": 2,
        "agent_count": 4,
    }
    changed = running.patch(f"/api/worlds/{world}", json={"monthly_budget_usd": 25, "run_cap": 1})
    assert changed.status_code == 200
    assert (changed.json()["monthly_budget_usd"], changed.json()["run_cap"]) == (25.0, 1)
    assert changed.json()["agent_count"] == 4  # untouched
    queue = DBOS.retrieve_queue(queue_name(world))
    assert queue is not None and queue._concurrency == 1  # the cap reached DBOS's queue

    cleared = running.patch(
        f"/api/worlds/{world}", json={"monthly_budget_usd": None, "retention_days": 0}
    )
    assert cleared.json()["monthly_budget_usd"] is None and cleared.json()["retention_days"] == 0
    events = [e for e in world_service(tmp_path).events_after(world) if e.type == "world.updated"]
    assert [e.payload for e in events] == [
        {"id": world, "monthly_budget_usd": 25.0, "run_cap": 1},
        {"id": world, "monthly_budget_usd": None, "retention_days": 0},
    ]
    for bad in (
        {"run_cap": 0},
        {"agent_count": 9},
        {"retention_days": -1},
        {"monthly_budget_usd": 0},
    ):
        assert running.patch(f"/api/worlds/{world}", json=bad).status_code == 422
    assert running.patch("/api/worlds/world_nope", json={"run_cap": 2}).status_code == 404


def test_the_skin_view_is_read_and_written_through_the_settings(running: TestClient) -> None:
    world = running.post("/api/worlds", json={"name": "Video"}).json()
    assert world["skin"] == {"view": "plain"}
    changed = running.patch(f"/api/worlds/{world['id']}", json={"skin": {"view": "office"}})
    assert changed.status_code == 200 and changed.json()["skin"] == {"view": "office"}
    assert running.get(f"/api/worlds/{world['id']}").json()["skin"] == {"view": "office"}
    for bad in ({"view": "map"}, {"view": "office", "theme": "dark"}, None):
        assert running.patch(f"/api/worlds/{world['id']}", json={"skin": bad}).status_code == 422
    assert running.get(f"/api/worlds/{world['id']}").json()["skin"] == {"view": "office"}


def test_the_cast_has_defaults_and_edits_need_confirmation_and_are_versioned(
    running: TestClient, tmp_path: Path
) -> None:
    world = _world(running)
    cast = running.get(f"/api/worlds/{world}/cast").json()
    assert [(m["name"], m["role"], m["version"]) for m in cast] == [
        ("planner", "planner", 1),
        ("worker-1", "worker", 1),
        ("worker-2", "worker", 1),
        ("worker-3", "worker", 1),
        ("worker-4", "worker", 1),
        ("judge", "judge", 1),
    ]
    assert cast[1]["record"] == {
        "tasks_done": 0,
        "judged": 0,
        "first_try": 0,
        "send_backs": 0,
        "cost_usd": 0.0,
    }

    change = {"prompt": "Write hooks first.", "tools": ["web_search", "graph"]}
    refused = running.put(f"/api/worlds/{world}/cast/worker-1", json=change)
    assert refused.status_code == 422 and "confirm" in refused.json()["detail"]
    assert _cast(running, world)["worker-1"]["version"] == 1

    edited = running.put(f"/api/worlds/{world}/cast/worker-1", json={**change, "confirm": True})
    assert edited.status_code == 200
    assert (edited.json()["version"], edited.json()["prompt"]) == (2, "Write hooks first.")
    assert edited.json()["tools"] == ["web_search", "graph"] and edited.json()["updated_at"]
    event = world_service(tmp_path).events_after(world)[-1]
    assert event.type == "cast.updated" and event.payload["version"] == 2

    again = running.put(f"/api/worlds/{world}/cast/worker-1", json={**change, "confirm": True})
    assert again.status_code == 422 and again.json()["detail"] == "nothing changed"
    versions = running.get(f"/api/worlds/{world}/cast/worker-1/versions").json()
    assert [(v["number"], v["prompt"], v["tools"]) for v in versions] == [
        (1, "", None),
        (2, "Write hooks first.", ["web_search", "graph"]),
    ]
    assert (
        running.put(f"/api/worlds/{world}/cast/worker-9", json={"confirm": True}).status_code == 404
    )
    assert running.get(f"/api/worlds/{world}/cast/intern/versions").status_code == 404
    assert running.get("/api/worlds/world_nope/cast").status_code == 404

    running.patch(f"/api/worlds/{world}", json={"agent_count": 2})
    assert [m["name"] for m in running.get(f"/api/worlds/{world}/cast").json()] == [
        "planner",
        "worker-1",
        "worker-2",
        "judge",
    ]


def test_the_record_counts_tasks_first_try_passes_send_backs_and_cost(
    running: TestClient, script: Script
) -> None:
    script.scripts = [PLAN, WORK, PASS, [CLAIM, FINISH], PASS]
    script.cost = 0.5
    world = _world(running)
    yaml = RESEARCH + "  - gate: review\n    ask: Good?\n    kind: approve\n"
    run = _run_research(running, world, yaml)
    url = f"/api/runs/{run['id']}"
    _until(lambda: running.get(url).json()["status"] == "waiting_gate")

    cast = _cast(running, world)
    assert cast["worker-1"]["record"] == {
        "tasks_done": 1,
        "judged": 1,
        "first_try": 1,
        "send_backs": 0,
        "cost_usd": 0.5,
    }
    assert cast["planner"]["record"]["cost_usd"] == 0.5
    assert cast["judge"]["record"]["cost_usd"] == 0.5

    sent = running.post(
        f"{url}/decision", json={"note": "Fewer ideas.", "send_back_to": "research"}
    )
    assert sent.status_code == 200
    _until(
        lambda: (
            running.get(url).json()["send_backs"] == 1
            and running.get(url).json()["status"] == "waiting_gate"
        )
    )
    record = _cast(running, world)["worker-1"]["record"]
    assert (record["tasks_done"], record["send_backs"], record["cost_usd"]) == (2, 1, 1.0)
    assert (record["judged"], record["first_try"]) == (1, 1)  # the stage's first verdict passed


def test_an_edited_prompt_and_tools_reach_the_next_stage_that_starts(
    running: TestClient, script: Script
) -> None:
    script.scripts = [PLAN, WORK, PASS]
    world = _world(running)
    change = {"prompt": "Write hooks first.", "tools": ["graph"], "confirm": True}
    assert running.put(f"/api/worlds/{world}/cast/worker-1", json=change).status_code == 200
    judge = {"prompt": "Be strict about sources.", "confirm": True}
    assert running.put(f"/api/worlds/{world}/cast/judge", json=judge).status_code == 200
    run = _run_research(running, world)
    assert _ended(running, run["id"])["status"] == "done"
    worker, judged = script.sessions[1], script.sessions[2]
    assert "Your standing instructions: Write hooks first." in str(worker.system_prompt)
    assert "WebSearch" not in worker.allowed_tools  # the stage allows it, worker-1 may not
    assert tool_id("query_graph") in worker.allowed_tools
    assert "Your standing instructions: Be strict about sources." in str(judged.system_prompt)


def test_an_ad_hoc_run_uses_the_worlds_connectors_and_feeds_its_graph(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, WORK, PASS]
    world = _world(running)
    assert running.post(f"/api/worlds/{world}/connectors", json=NOTION).status_code == 201
    started = running.post(
        f"/api/worlds/{world}/runs",
        json={"goal": "Find five hooks for next week's short.\nShort ones."},
    )
    assert started.status_code == 201, started.text
    run = started.json()
    assert (run["workflow_id"], run["workflow_version"]) == (None, None)
    assert run["name"] == "Find five hooks for next week's short."
    assert [(s["name"], s["type"]) for s in run["stages"]] == [("goal", "swarm")]
    assert _ended(running, run["id"])["status"] == "done"

    worker = script.sessions[1]
    assert "WebSearch" in worker.allowed_tools and "mcp__notion__*" in worker.allowed_tools
    servers = worker.mcp_servers
    assert isinstance(servers, dict) and "notion" in servers  # beside the in-process tools
    assert "Goal: Find five hooks" in str(worker.system_prompt)
    assert "Done when: The goal is met" in str(worker.system_prompt)
    stage = run["stages"][0]["id"]
    note = tmp_path / "worlds" / world / "runs" / run["id"] / stage / "notes" / "trends.md"
    assert "Hooks in the first second." in note.read_text()
    events = world_service(tmp_path)
    _until(lambda: any(e.type == "graph.rebuilt" for e in events.events_after(world)))
    assert running.get(f"/api/worlds/{world}/graph").json()["nodes"] > 0

    assert [r["id"] for r in running.get(f"/api/worlds/{world}/runs").json()] == [run["id"]]
    assert running.get(f"/api/runs/{run['id']}/baseline").json() is None
    assert running.post(f"/api/worlds/{world}/runs", json={"goal": "  "}).status_code == 422
    assert running.post("/api/worlds/world_nope/runs", json={"goal": "Hi"}).status_code == 404


def test_the_monthly_budget_caps_each_run_and_refuses_runs_once_spent(
    running: TestClient, script: Script
) -> None:
    script.scripts = [PLAN, WORK, PASS]
    script.cost = 0.5  # three sessions: $1.50 per run
    world = _world(running)
    assert (
        running.patch(f"/api/worlds/{world}", json={"monthly_budget_usd": 2.0}).status_code == 200
    )
    run = _run_research(running, world)
    assert run["budget_usd"] == 2.0  # no budget asked for: what is left of the month
    assert _ended(running, run["id"])["status"] == "done"
    spend = running.get(f"/api/worlds/{world}/spend").json()
    assert (spend["spent_usd"], spend["monthly_budget_usd"]) == (1.5, 2.0)
    assert len(spend["month"]) == 7

    wf = running.get(f"/api/worlds/{world}/workflows").json()[0]["id"]
    capped = running.post(f"/api/workflows/{wf}/runs", json={"budget_usd": 5.0})
    assert capped.status_code == 201 and capped.json()["budget_usd"] == 0.5
    _ended(running, capped.json()["id"])

    running.patch(f"/api/worlds/{world}", json={"monthly_budget_usd": 1.0})
    refused = running.post(f"/api/workflows/{wf}/runs", json={})
    assert refused.status_code == 409
    assert "monthly budget of $1.00 is spent" in refused.json()["detail"]
    ad_hoc = running.post(f"/api/worlds/{world}/runs", json={"goal": "Anything"})
    assert ad_hoc.status_code == 409
    assert running.get("/api/worlds/world_nope/spend").status_code == 404


def test_the_most_specific_model_wins_stage_then_role_then_world_then_global(
    running: TestClient, script: Script
) -> None:
    world = _world(running)
    models = running.get("/api/models").json()
    assert models["default"] == "claude-opus-5-5"
    assert "claude-sonnet-5-5" in models["models"] and "claude-haiku-4-5" in models["models"]

    def ran(yaml: str = RESEARCH) -> dict[str, str]:
        script.scripts = [PLAN, WORK, PASS]
        script.sessions.clear()
        run = _run_research(running, world, yaml)
        assert _ended(running, run["id"])["status"] == "done"
        assert len(script.sessions) == 3  # planner, worker, judge
        agents = running.get(f"/api/runs/{run['id']}/agents").json()
        return {a["name"]: a["model"] for a in agents}

    assert ran() == dict.fromkeys(("planner", "worker-1", "judge"), "claude-opus-5-5")
    assert running.patch(f"/api/worlds/{world}", json={"model": "claude-sonnet-5-5"}).is_success
    assert ran() == dict.fromkeys(("planner", "worker-1", "judge"), "claude-sonnet-5-5")
    roles = {"planner_model": "claude-haiku-4-5", "judge_model": "claude-opus-5-5"}
    assert running.patch(f"/api/worlds/{world}", json=roles).is_success
    assert ran() == {
        "planner": "claude-haiku-4-5",
        "worker-1": "claude-sonnet-5-5",
        "judge": "claude-opus-5-5",
    }
    staged = ran(RESEARCH + "    model: claude-haiku-4-5\n")
    assert staged == dict.fromkeys(("planner", "worker-1", "judge"), "claude-haiku-4-5")
    assert [o.model for o in script.sessions] == ["claude-haiku-4-5"] * 3
    bad = running.patch(f"/api/worlds/{world}", json={"model": "two words"})
    assert bad.status_code == 422


def test_a_swarm_stage_runs_one_agent_unless_it_or_the_start_asks_for_more(
    running: TestClient, script: Script
) -> None:
    """ADR 0020: one worker by default though the world's cast has 4; a stage's `agents` runs
    that many over the shared whiteboard as before; a start's `agents` overrides the stage's."""
    world = _world(running)
    plan_two = [Answer({"tasks": [{"title": "Find hooks."}, {"title": "Find trends."}]})]
    alone = [GRAPH, CLAIM, NOTE, FINISH, CLAIM, FINISH, Say("Both done.")]
    one_each = [GRAPH, CLAIM, NOTE, FINISH, Say("Mine done.")]
    two = RESEARCH.replace("tools: [web_search]", "tools: [web_search]\n    agents: 2")

    def workers(yaml: str, body: dict, *work: list) -> list[str]:
        script.scripts = [plan_two, *work, PASS]
        script.sessions.clear()
        wf = running.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
        run = running.post(f"/api/workflows/{wf['workflow_id']}/runs", json=body).json()
        ended = _ended(running, run["id"])
        assert ended["status"] == "done", [s.get("error") for s in ended["stages"]]
        # Saved as a workflow, the stage keeps the worker count it ran with.
        saved = running.post(f"/api/runs/{run['id']}/workflow", json={})
        assert saved.status_code == 201, saved.text
        (stage,) = saved.json()["model"]["stages"]
        assert ("agents:" in saved.json()["yaml"]) == (stage["agents"] > 1)
        agents = running.get(f"/api/runs/{run['id']}/agents").json()
        names = sorted(a["name"] for a in agents if a["name"].startswith("worker"))
        assert stage["agents"] == len(names)
        return names

    assert workers(RESEARCH, {}, alone) == ["worker-1"]
    assert workers(two, {}, one_each, one_each) == ["worker-1", "worker-2"]
    assert workers(two.replace("Research", "Other"), {"agents": 1}, alone) == ["worker-1"]
    too_many = running.post("/api/workflows/wf_nope/runs", json={"agents": 9})
    assert too_many.status_code == 422


def test_a_saved_ad_hoc_run_keeps_the_tasks_added_by_hand_in_its_goal(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    """Story 21: a task the operator added to the goal stage is part of what the run did, so
    the saved workflow's goal names it; tasks the agents added are theirs to plan again."""
    from swarmify_api.main import task_board

    script.scripts = [PLAN, WORK, PASS]
    world = _world(running)
    run = running.post(f"/api/worlds/{world}/runs", json={"goal": "Find hooks."}).json()
    assert _ended(running, run["id"])["status"] == "done"
    stage = run["stages"][0]["id"]
    # Added while the run went (the HTTP add refuses an ended stage, so straight to the board).
    task_board(tmp_path).add_task(
        world, run["id"], stage, "Check the niche", added_by="operator", body="Shorts only."
    )
    saved = running.post(f"/api/runs/{run['id']}/workflow", json={})
    assert saved.status_code == 201, saved.text
    (goal,) = saved.json()["model"]["stages"]
    assert goal["goal"] == "Find hooks.\n\nAlso do these tasks:\n- Check the niche: Shorts only."
    assert "Find 5 ideas" not in goal["goal"]  # the planner's task
