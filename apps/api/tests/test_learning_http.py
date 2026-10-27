"""Learning over HTTP: real DBOS on SQLite, the real Agent SDK client with a scripted CLI behind
its transport (the retro session included), skills as files, the fake GraphIndex."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_connectors import FakeTransport
from swarmify_memory import FakeGraphIndex
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    output_type: list[idea]
  - gate: review
    ask: Good?
    kind: approve
"""
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
WORK = [
    Call(tool_id("query_graph"), {"question": "What did we reject before?"}),
    Call(tool_id("claim_task")),
    Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks in the first second win."}),
    Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."}),
    Say("Five ideas."),
]
IDEAS = [{"title": "Hook first", "body": "Open on the hook.", "rank": 1, "sources": []}]
PASS = [Answer({"passed": True, "reasons": ["Five ideas."], "items": IDEAS})]
QUIET = [Answer({"skill": None, "workflow": None})]
HOOKS = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n"
PROPOSAL = {
    "skill": {
        "action": "new",
        "name": "hooks",
        "content": HOOKS,
        "summary": "Hook first",
        "evidence": ["review: sent back with the note: Hook sooner."],
    },
    "workflow": None,
}
CITE = "---\nname: cite\ndescription: Cite sources.\n---\n\nName the note.\n"


@pytest.fixture
def app(tmp_path: Path, transport: FakeTransport, script: Script) -> FastAPI:
    return create_app(
        tmp_path,
        transport=transport,
        poll_seconds=0.01,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=FakeGraphIndex(),
        extract=fake_extract,
    )


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def get(client: TestClient, run_id: str) -> dict[str, Any]:
    return client.get(f"/api/runs/{run_id}").json()


def at_gate(client: TestClient, run_id: str, stage: str) -> dict[str, Any]:
    def check() -> bool:
        run = get(client, run_id)
        return run["status"] == "waiting_gate" and any(
            s["name"] == stage and s["status"] == "waiting" for s in run["stages"]
        )

    until(check)
    return get(client, run_id)


def settled(client: TestClient, run_id: str) -> dict[str, Any]:
    """The run once it is done or waits at its retro gate."""

    def check() -> bool:
        run = get(client, run_id)
        waiting = [s["name"] for s in run["stages"] if s["status"] == "waiting"]
        return run["status"] == "done" or waiting == ["retro"]

    until(check)
    return get(client, run_id)


def setup(client: TestClient, yaml: str = RESEARCH) -> tuple[str, str]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    created = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert created.status_code == 201, created.json()
    return world, created.json()["workflow_id"]


def run_to_review(client: TestClient, workflow: str, approve: dict | None = None) -> dict:
    run = client.post(f"/api/workflows/{workflow}/runs", json={}).json()
    at_gate(client, run["id"], "review")
    answer = client.post(f"/api/runs/{run['id']}/decision", json=approve or {})
    assert answer.status_code == 200, answer.json()
    return run


def test_every_run_ends_with_a_retro_whose_proposal_waits_at_a_skill_gate(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    shared = tmp_path / "skills" / "cite" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_text(CITE)
    # Sent back once: the worker and judge run again, then the gate again, then the retro.
    script.scripts = [PLAN, WORK, PASS, WORK, PASS, [Answer(PROPOSAL)]]
    world, workflow = setup(running)
    run = run_to_review(running, workflow, {"note": "Hook sooner.", "send_back_to": "research"})
    assert [s["type"] for s in run["stages"]] == ["swarm", "gate", "retro"]
    url = f"/api/runs/{run['id']}"
    until(lambda: get(running, run["id"])["send_backs"] == 1)
    at_gate(running, run["id"], "review")
    assert running.post(f"{url}/decision", json={}).status_code == 200
    waiting = at_gate(running, run["id"], "retro")
    assert waiting["stages"][2]["status"] == "waiting"

    (gate,) = running.get("/api/gates").json()
    assert (gate["run_id"], gate["stage_name"], gate["kind"]) == (run["id"], "retro", "skill")
    assert "Approve it, edit it, or reject it" in gate["ask"]
    proposal = running.get(f"{url}/proposal").json()
    assert proposal["status"] == "pending" and proposal["workflow"] is None
    assert proposal["skill"]["name"] == "hooks" and proposal["skill"]["action"] == "new"
    assert proposal["skill"]["evidence"] == ["review: sent back with the note: Hook sooner."]
    retro_prompt = script.prompts[-1]
    assert "sent back to research with the note: Hook sooner." in retro_prompt
    assert "research loop 1: passed, because Five ideas." in retro_prompt
    assert "## cite" in retro_prompt, "the shared skill the world loads is in front of the retro"
    retro = next(a for a in running.get(f"{url}/agents").json() if a["name"] == "retro")
    assert retro["status"] == "done" and retro["skills"] == [f"{world}:cite"]
    assert retro["stage_id"] == waiting["stages"][2]["id"]
    library = running.get(f"/api/worlds/{world}/library").json()
    (card,) = [i for i in library if i["type"] == "skill_proposal"]
    assert card["stage_name"] == "retro" and card["title"] == "new skill hooks: Hook first"
    assert "worker-1" in {a["name"] for a in running.get(f"{url}/agents").json()}

    # A decision through the runs API alone would skip the skill: the proposal endpoint does both.
    refused = running.post(f"{url}/decision", json={"choice": "approve"})
    assert refused.status_code == 409 and "proposal" in refused.json()["detail"]
    edited = HOOKS.replace("Write the hook first.", "Write the hook in the first second.")
    body = {"approve": True, "content": edited, "note": "Tightened the wording"}
    decided = running.post(f"/api/proposals/{proposal['id']}/decide", json=body)
    assert decided.status_code == 200, decided.json()
    assert (
        decided.json()["status"] == "approved" and decided.json()["note"] == "Tightened the wording"
    )
    until(lambda: get(running, run["id"])["status"] == "done")
    done = get(running, run["id"])
    assert [d["kind"] for d in done["decisions"]] == ["approve", "approve", "skill"]
    assert done["decisions"][-1]["choice"] == "approve"
    assert running.get("/api/gates").json() == []
    file = tmp_path / "worlds" / world / "skills" / "hooks" / "SKILL.md"
    assert file.read_text() == edited and not file.parent.is_symlink()

    skills = running.get(f"/api/worlds/{world}/skills").json()
    assert [(s["name"], s["source"], s["version"], s["run_id"]) for s in skills] == [
        ("cite", "shared", None, None),
        ("hooks", "world", 1, run["id"]),
    ]
    assert skills[1]["description"] == "Open on the hook." and skills[1]["content"] == edited
    (version,) = running.get(f"/api/worlds/{world}/skills/hooks/versions").json()
    assert (version["number"], version["run_id"], version["note"]) == (
        1,
        run["id"],
        "Tightened the wording",
    )
    again = running.post(f"/api/proposals/{proposal['id']}/decide", json=body)
    assert again.status_code == 422 and "already approved" in again.json()["detail"]
    assert running.post("/api/proposals/prop_nope/decide", json=body).status_code == 404
    assert running.get(f"{url}/proposal").status_code == 404
    assert [p["status"] for p in running.get(f"/api/worlds/{world}/proposals").json()] == [
        "approved"
    ]

    # The next run's agents load the new skill, world winning over a shared one of that name.
    (tmp_path / "skills" / "hooks").mkdir()
    (tmp_path / "skills" / "hooks" / "SKILL.md").write_text(CITE.replace("cite", "hooks"))
    sessions = len(script.sessions)
    script.scripts = [[]] * sessions + [PLAN, WORK, PASS, QUIET]  # the next sessions, in order
    second = run_to_review(running, workflow)
    until(lambda: get(running, second["id"])["status"] == "done")
    worker = script.sessions[sessions + 1]
    assert worker.plugins == [{"type": "local", "path": str(tmp_path / "worlds" / world)}]
    agents = running.get(f"/api/runs/{second['id']}/agents").json()
    (worker_agent,) = [a for a in agents if a["name"] == "worker-1"]
    assert worker_agent["skills"] == [f"{world}:cite", f"{world}:hooks"]
    assert (tmp_path / "worlds" / world / "skills" / "hooks" / "SKILL.md").read_text() == edited
    assert get(running, second["id"])["decisions"][-1]["kind"] == "approve", "no gate: quiet"


def test_a_rejection_records_why_and_a_baseline_run_has_no_retro(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, WORK, PASS, [Answer(PROPOSAL)]]
    world, workflow = setup(running)
    run = run_to_review(running, workflow)
    at_gate(running, run["id"], "retro")
    proposal = running.get(f"/api/runs/{run['id']}/proposal").json()
    bad = {"approve": True, "content": "not a skill"}
    refused = running.post(f"/api/proposals/{proposal['id']}/decide", json=bad)
    assert refused.status_code == 422 and "frontmatter" in refused.json()["detail"]
    rejected = running.post(
        f"/api/proposals/{proposal['id']}/decide", json={"approve": False, "note": "Not the gap."}
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert rejected.json()["note"] == "Not the gap."
    until(lambda: get(running, run["id"])["status"] == "done")
    assert get(running, run["id"])["decisions"][-1]["choice"] == "reject"
    assert running.get(f"/api/worlds/{world}/skills").json() == []
    assert not (tmp_path / "worlds" / world / "skills" / "hooks").exists()

    sessions = len(script.sessions)
    baseline = running.post(f"/api/workflows/{workflow}/runs", json={"baseline": True}).json()
    assert [s["type"] for s in baseline["stages"]] == ["swarm", "gate"]
    script.scripts = [WORK]
    at_gate(running, baseline["id"], "review")
    assert running.post(f"/api/runs/{baseline['id']}/decision", json={}).status_code == 200
    until(lambda: get(running, baseline["id"])["status"] == "done")
    assert len(script.sessions) == sessions + 1, "one worker, no retro"
    ad_hoc = running.post(f"/api/worlds/{world}/runs", json={"goal": "Find hooks."}).json()
    assert [s["type"] for s in ad_hoc["stages"]] == ["swarm", "retro"]
    assert running.post(f"/api/runs/{ad_hoc['id']}/abort").status_code == 200


def test_a_library_artifact_becomes_a_skill_draft_the_operator_saves(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts, script.cycle = [PLAN, WORK, PASS, QUIET], True
    world, workflow = setup(running)
    run = run_to_review(running, workflow)
    until(lambda: get(running, run["id"])["status"] == "done")
    library = running.get(f"/api/worlds/{world}/library").json()
    idea = next(i for i in library if i["type"] == "idea")
    draft = f"---\nname: hook-first\ndescription: {idea['title']}\n---\n\n{idea['body']}"
    body = {"name": "hook-first", "content": draft, "run_id": idea["run_id"], "note": idea["path"]}
    saved = running.post(f"/api/worlds/{world}/skills", json=body)
    assert saved.status_code == 201, saved.json()
    skill = saved.json()
    assert (skill["name"], skill["source"], skill["version"], skill["run_id"]) == (
        "hook-first", "world", 1, run["id"],
    )  # fmt: skip
    assert skill["description"] == "Hook first"
    assert (tmp_path / "worlds" / world / "skills" / "hook-first" / "SKILL.md").is_file()
    second = running.post(f"/api/worlds/{world}/skills", json={**body, "run_id": None})
    assert second.json()["version"] == 2
    versions = running.get(f"/api/worlds/{world}/skills/hook-first/versions").json()
    assert [(v["number"], v["run_id"]) for v in versions] == [(1, run["id"]), (2, None)]
    wrong = running.post(f"/api/worlds/{world}/skills", json={"name": "Hook", "content": draft})
    assert wrong.status_code == 422 and "not a skill name" in wrong.json()["detail"]
    assert running.post("/api/worlds/world_nope/skills", json=body).status_code == 404
    assert running.get("/api/worlds/world_nope/skills").status_code == 404

    # The next run's agents load it, and the cast of the world lists it with its run.
    sessions = len(script.sessions)
    next_run = run_to_review(running, workflow)
    until(lambda: get(running, next_run["id"])["status"] == "done")
    agents = running.get(f"/api/runs/{next_run['id']}/agents").json()
    assert all(a["skills"] == [f"{world}:hook-first"] for a in agents), agents
    assert len(script.sessions) == sessions + 4


def test_the_same_send_back_in_three_runs_brings_a_workflow_suggestion_the_gate_can_apply(
    running: TestClient, script: Script
) -> None:
    yaml_v2 = RESEARCH.replace("Find 5 ideas trending this week.", "Find 5 short ideas with hooks.")
    suggestion = {
        "skill": None,
        "workflow": {
            "summary": "Ask for hooks up front",
            "yaml": yaml_v2,
            "evidence": ["send_back at review: research in 3 runs"],
        },
    }
    # Each run: plan, work, judge, rework after the send-back, judge, retro. The retro offers the
    # suggestion every time; Learning keeps it only once the send-back recurred in three runs.
    script.scripts, script.cycle = [PLAN, WORK, PASS, WORK, PASS, [Answer(suggestion)]], True
    world, workflow = setup(running)
    runs = []
    for n in range(3):
        run = run_to_review(running, workflow, {"note": "Hooks!", "send_back_to": "research"})
        until(lambda r=run: get(running, r["id"])["send_backs"] == 1)
        at_gate(running, run["id"], "review")
        assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
        runs.append(run)
        assert settled(running, run["id"])["status"] == ("waiting_gate" if n == 2 else "done")
    proposals = running.get(f"/api/worlds/{world}/proposals").json()
    waiting = [p for p in proposals if p["status"] == "pending"]
    assert waiting, "the third run's retro saw the recurrence and proposed the workflow change"
    (proposal,) = waiting
    assert proposal["skill"] is None and proposal["workflow"]["summary"] == "Ask for hooks up front"
    prompt = next(p for p in reversed(script.prompts) if "Retro of run" in p)
    assert "repeated in 3 or more runs" in prompt and "send_back at review: research" in prompt
    assert RESEARCH.strip() in prompt

    applied = running.post(
        f"/api/proposals/{proposal['id']}/decide",
        json={"approve": True, "apply_workflow": True, "note": "Yes"},
    )
    assert applied.status_code == 200, applied.json()
    assert running.get(f"/api/workflows/{workflow}").json()["current_version"] == 2
    again = running.post(
        f"/api/proposals/{proposal['id']}/decide",
        json={"approve": True, "apply_workflow": True, "note": "Yes"},
    )
    assert again.status_code == 422, "a decided proposal is refused"
    assert running.get(f"/api/workflows/{workflow}").json()["current_version"] == 2
    v2 = running.get(f"/api/workflows/{workflow}/versions/2").json()
    assert "Find 5 short ideas with hooks." in v2["yaml"]
    until(lambda: get(running, proposal["run_id"])["status"] == "done")

    card = running.get(f"/api/workflows/{workflow}/scorecard").json()
    assert (card["workflow_id"], card["world_id"], card["name"]) == (workflow, world, "Research")
    scores = card["runs"]
    assert [s["run_id"] for s in scores] == [r["id"] for r in runs]
    for s in scores:
        assert (s["judged"], s["first_try"], s["send_backs"], s["status"]) == (1, 1, 1, "done")
        assert s["cost_usd"] > 0 and s["duration_s"] is not None and s["views"] is None
        assert s["workflow_version"] == 1 and s["baseline"] is False
    assert running.get("/api/workflows/wf_nope/scorecard").status_code == 404


def test_an_invalid_workflow_suggestion_is_refused_and_nothing_is_decided(
    running: TestClient, script: Script
) -> None:
    broken = {
        "skill": None,
        "workflow": {"summary": "Break it", "yaml": "name: x\nstages: []\n", "evidence": ["e"]},
    }
    script.scripts, script.cycle = [PLAN, WORK, PASS, WORK, PASS, [Answer(broken)]], True
    world, workflow = setup(running)
    for _ in range(3):
        run = run_to_review(running, workflow, {"note": "Hooks!", "send_back_to": "research"})
        until(lambda r=run: get(running, r["id"])["send_backs"] == 1)
        at_gate(running, run["id"], "review")
        assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
        settled(running, run["id"])
    (proposal,) = [
        p for p in running.get(f"/api/worlds/{world}/proposals").json() if p["status"] == "pending"
    ]
    refused = running.post(
        f"/api/proposals/{proposal['id']}/decide", json={"approve": True, "apply_workflow": True}
    )
    assert refused.status_code == 422 and "refused" in refused.json()["detail"]
    assert running.get(f"/api/workflows/{workflow}").json()["current_version"] == 1
    assert running.get(f"/api/runs/{proposal['run_id']}/proposal").json()["status"] == "pending"
    kept = running.post(f"/api/proposals/{proposal['id']}/decide", json={"approve": True})
    assert kept.status_code == 200 and kept.json()["status"] == "approved"
    until(lambda: get(running, proposal["run_id"])["status"] == "done")


def test_a_done_run_with_a_retro_saves_as_a_workflow_without_it(
    running: TestClient, script: Script
) -> None:
    script.scripts = [QUIET]
    _, workflow = setup(running, "name: Check\nstages:\n  - gate: ok\n    ask: Fine?\n")
    run = running.post(f"/api/workflows/{workflow}/runs", json={}).json()
    assert [s["type"] for s in run["stages"]] == ["gate", "retro"]
    at_gate(running, run["id"], "ok")
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: get(running, run["id"])["status"] == "done")
    saved = running.post(f"/api/runs/{run['id']}/workflow", json={})
    assert saved.status_code == 201, saved.text
    assert "retro" not in saved.json()["yaml"]
    assert [s["type"] for s in saved.json()["model"]["stages"]] == ["gate"]
