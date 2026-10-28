"""The Library, pins, forks and promotes over HTTP: real DBOS on SQLite, a scripted agent CLI
behind the Agent SDK and the fake ConnectorTransport."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import FakeTransport
from swarmify_swarm import tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

RENDER = {
    "name": "render_video",
    "kind": "stage",
    "server": "uvx render-mcp",
    "stage_types": ["connector"],
}
SHORT = """name: Short
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    output_type: list[idea]
    produces: [ideas.md]
  - gate: pick
    ask: Pick one.
    kind: pick
  - connector: render
    calls: render_video
    input: ideas.md
"""
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
WORK = [
    Call(tool_id("query_graph"), {"question": "What did we reject before?"}),
    Call(tool_id("claim_task")),
    Call(tool_id("write_note"), {"title": "Findings", "body": "Hooks in the first second win."}),
    Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks\n2. Chase"}),
    Call(tool_id("finish_task"), {"task_id": LAST, "note": "Found two."}),
    Say("Two ideas."),
]
IDEAS = [
    {"title": "Hook first", "body": "Open on the hook.", "rank": 1, "sources": []},
    {"title": "The chase", "body": "A chase in 30 seconds.", "rank": 2, "sources": []},
]
PASS = [Answer({"passed": True, "reasons": ["Two ideas, ranked."], "items": IDEAS})]


@pytest.fixture
def running(app: FastAPI, script: Script) -> Iterator[TestClient]:
    script.scripts, script.cycle = [PLAN, WORK, PASS], True
    with TestClient(app) as client:
        yield client


def setup(client: TestClient) -> tuple[str, str]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=RENDER).status_code == 201
    created = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SHORT})
    assert created.status_code == 201, created.json()
    return world, created.json()["workflow_id"]


def get(client: TestClient, run_id: str) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


def run_to_done(client: TestClient, workflow: str) -> dict:
    run = client.post(f"/api/workflows/{workflow}/runs", json={}).json()
    until(lambda: get(client, run["id"])["status"] == "waiting_gate")
    assert client.post(f"/api/runs/{run['id']}/decision", json={"choice": "1"}).status_code == 200
    until(lambda: get(client, run["id"])["status"] == "done")
    return get(client, run["id"])


def test_the_library_lists_every_artifact_with_its_run_and_stage_and_pins_stick(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    world, workflow = setup(running)
    run = run_to_done(running, workflow)
    research, pick = run["stages"][0], run["stages"][1]

    items = running.get(f"/api/worlds/{world}/library").json()
    by_name = {i["name"]: i for i in items}
    assert {n: (i["type"], i["stage_name"], i["stage_index"]) for n, i in by_name.items()} == {
        "idea-hook-first.md": ("idea", "research", 0),
        "idea-the-chase.md": ("idea", "research", 0),
        "ideas.md": ("file", "research", 0),
        "verdict-1.md": ("verdict", "research", 0),
        "decision.md": ("decision", "pick", 1),
    }
    idea = by_name["idea-hook-first.md"]
    assert (idea["run_id"], idea["run_name"], idea["workflow_id"]) == (run["id"], "Short", workflow)
    assert idea["workflow_version"] == 1 and idea["fields"]["rank"] == 1 and idea["created"]
    assert idea["path"] == f"runs/{run['id']}/{research['id']}/artifacts/idea-hook-first.md"
    assert by_name["decision.md"]["path"] == f"runs/{run['id']}/{pick['id']}/decision.md"
    assert pick["name"] == "pick"
    assert not any(i["pinned"] for i in items)
    assert running.get("/api/worlds/world_nope/library").status_code == 404

    pinned = running.post(f"/api/worlds/{world}/pins", json={"path": idea["path"]})
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True
    assert pinned.json()["stage_name"] == "research"
    assert "pinned: true" in (tmp_path / "worlds" / world / idea["path"]).read_text()
    listed = running.get(f"/api/worlds/{world}/library").json()
    assert [i["name"] for i in listed if i["pinned"]] == ["idea-hook-first.md"]
    missing = {"path": f"runs/{run['id']}/{research['id']}/artifacts/nope.md"}
    assert running.post(f"/api/worlds/{world}/pins", json=missing).status_code == 404

    # The pin reaches the planner and the worker of the next swarm stage.
    sessions = len(script.sessions)
    run_to_done(running, workflow)
    planner, worker = script.prompts[sessions], script.sessions[sessions + 1]
    expected = str(tmp_path / "worlds" / world / idea["path"])
    assert "Pinned references, required reading" in planner and expected in planner
    assert expected in str(worker.system_prompt)

    off = {"path": idea["path"], "pinned": False}
    unpinned = running.post(f"/api/worlds/{world}/pins", json=off)
    assert unpinned.json()["pinned"] is False
    assert not any(i["pinned"] for i in running.get(f"/api/worlds/{world}/library").json())


def test_a_fork_keeps_the_copied_stages_and_runs_on_without_repeating_them(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    world, workflow = setup(running)
    source = run_to_done(running, workflow)
    sessions, renders = len(script.sessions), len(transport.calls)

    refused = running.post(f"/api/runs/{source['id']}/fork", json={"stage": 7})
    assert refused.status_code == 422
    assert running.post("/api/runs/run_nope/fork", json={"stage": 0}).status_code == 404

    forked = running.post(f"/api/runs/{source['id']}/fork", json={"stage": 1})
    assert forked.status_code == 201, forked.json()
    fork = forked.json()
    assert fork["forked_from"] == {"id": source["id"], "world_id": world, "name": "Short"}
    assert fork["fork_stage_index"] == 1 and fork["promoted_from"] is None
    assert [s["status"] for s in fork["stages"]] == ["done", "pending", "pending"]
    assert get(running, source["id"])["forks"] == [
        {"id": fork["id"], "world_id": world, "name": "Short"}
    ]
    copied = running.get(f"/api/runs/{fork['id']}/stages/{fork['stages'][0]['id']}/artifacts")
    assert sorted(a["name"] for a in copied.json()) == [
        "idea-hook-first.md",
        "idea-the-chase.md",
        "ideas.md",
        "verdict-1.md",
    ]
    library = running.get(f"/api/worlds/{world}/library").json()
    assert {i["run_id"] for i in library} == {source["id"], fork["id"]}

    until(lambda: get(running, fork["id"])["status"] == "waiting_gate")
    assert len(script.sessions) == sessions, "the copied swarm stage did not run again"
    assert running.post(f"/api/runs/{fork['id']}/decision", json={"choice": "2"}).status_code == 200
    until(lambda: get(running, fork["id"])["status"] == "done")
    assert len(transport.calls) == renders + 1 and len(script.sessions) == sessions
    runs = running.get(f"/api/worlds/{world}/runs").json()
    assert [(r["id"], (r["forked_from"] or {}).get("id")) for r in runs] == [
        (fork["id"], source["id"]),
        (source["id"], None),
    ]


def test_a_promote_starts_at_the_chosen_stage_with_the_artifact_as_input(
    running: TestClient, script: Script, transport: FakeTransport, tmp_path: Path
) -> None:
    world, workflow = setup(running)
    source = run_to_done(running, workflow)
    sessions, renders = len(script.sessions), len(transport.calls)
    ideas = next(
        i for i in running.get(f"/api/worlds/{world}/library").json() if i["name"] == "ideas.md"
    )

    for body, status in (
        ({"artifact": ideas["path"], "stage": 0}, 422),
        ({"artifact": ideas["path"], "stage": 3}, 422),
        ({"artifact": "../../../etc/hosts", "stage": 2}, 404),
        ({"artifact": ideas["path"] + ".nope", "stage": 2}, 404),
    ):
        assert running.post(f"/api/workflows/{workflow}/promote", json=body).status_code == status
    nope = running.post("/api/workflows/wf_nope/promote", json={"artifact": "x", "stage": 1})
    assert nope.status_code == 404

    promoted = running.post(
        f"/api/workflows/{workflow}/promote", json={"artifact": ideas["path"], "stage": 2}
    )
    assert promoted.status_code == 201, promoted.json()
    run = promoted.json()
    assert run["promoted_from"] == ideas["path"] and run["forked_from"] is None
    assert [s["status"] for s in run["stages"]] == ["skipped", "skipped", "pending"]
    placed = running.get(f"/api/runs/{run['id']}/stages/{run['stages'][1]['id']}/artifacts").json()
    assert [(a["name"], a["fields"]["run"]) for a in placed] == [("ideas.md", source["id"])]

    until(lambda: get(running, run["id"])["status"] == "done")
    ended = get(running, run["id"])
    assert [s["status"] for s in ended["stages"]] == ["skipped", "skipped", "done"]
    assert len(script.sessions) == sessions, "no swarm stage ran"
    assert len(transport.calls) == renders + 1
    assert transport.calls[-1][2]["input"] == "ideas.md"
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / run["stages"][1]["id"]
    assert (folder / "artifacts" / "ideas.md").read_text().endswith("2. Chase")


def test_a_promoted_typed_artifact_lands_under_the_name_the_stage_takes(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    world, _ = setup(running)
    scripted = SHORT.replace("ideas.md", "script.md")
    created = running.post(f"/api/worlds/{world}/workflows", json={"yaml": scripted})
    assert created.status_code == 201, created.json()
    rel = "runs/run_old/stage_x/artifacts/script-the-chase.md"
    (tmp_path / "worlds" / world / rel).parent.mkdir(parents=True)
    (tmp_path / "worlds" / world / rel).write_text('---\ntype: "script"\n---\n\nThe chase.')

    promoted = running.post(
        f"/api/workflows/{created.json()['workflow_id']}/promote",
        json={"artifact": rel, "stage": 2},
    )
    assert promoted.status_code == 201, promoted.json()
    run = promoted.json()
    until(lambda: get(running, run["id"])["status"] in ("done", "failed"))
    assert get(running, run["id"])["status"] == "done", get(running, run["id"])
    assert Path(transport.calls[-1][2]["input_path"]).read_text().endswith("The chase.")
