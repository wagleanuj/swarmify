"""A swarm stage over HTTP: real DBOS on SQLite, the real Agent SDK client with a scripted CLI
behind its transport, notes and artifacts on disk, and the live log on the world's stream."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_swarm import tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Die, Say, Script

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    tools: [web_search]
    output_type: list[idea]
    produces: [ideas.md]
"""
GRAPH = Call(tool_id("query_graph"), {"question": "What did we reject before?"})
CLAIM = Call(tool_id("claim_task"))
NOTE = Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks in the first second."})
IDEAS = Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"})
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
PASS = [Answer({"passed": True, "reasons": ["Five ideas."]})]


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
        PLAN,
        [GRAPH, CLAIM, NOTE, IDEAS, Call("Bash", {"command": "ls"}), FINISH, Say("Five ideas.")],
        PASS,
    ]
    script.cost = 0.042
    world, run = _start(running, tmp_path)
    ended = _ended(running, run["id"])
    assert ended["status"] == "done", ended
    stage = ended["stages"][0]["id"]

    log = running.get(f"/api/runs/{run['id']}/stages/{stage}/log").json()
    log = [e for e in log if e["agent"] == "worker-1"]
    allowed = [e["tool"] for e in log if e["kind"] == "tool"]
    assert allowed == [
        tool_id(n) for n in ("query_graph", "claim_task", "write_note", "write_artifact")
    ] + [tool_id("finish_task")]
    assert [e["tool"] for e in log if e["kind"] == "denied"] == ["Bash"]
    assert log[0]["kind"] == "session" and f"{world}:hooks" in log[0]["detail"]
    assert log[-1]["kind"] == "result" and "$0.0420" in log[-1]["detail"]

    planner, agent, judge = running.get(f"/api/runs/{run['id']}/agents").json()
    assert (planner["role"], judge["role"]) == ("planner", "judge")
    assert agent["name"] == "worker-1" and agent["status"] == "done"
    assert agent["cost_usd"] == 0.042 and agent["skills"] == [f"{world}:hooks"]
    assert agent["session_id"].startswith("session-")

    folder = tmp_path / "worlds" / world / "runs" / run["id"] / stage
    note = (folder / "notes" / "trends.md").read_text()
    assert 'author: "worker-1"' in note and "Hooks in the first second." in note
    assert (folder / "artifacts" / "ideas.md").read_text().endswith("1. Hooks")
    (task,) = running.get(f"/api/runs/{run['id']}/stages/{stage}/tasks").json()
    assert task["title"] == "Find 5 ideas trending this week." and task["owner"] == "worker-1"
    assert task["added_by"] == "planner" and task["status"] == "done"

    options = script.sessions[1]
    assert options.setting_sources == [] and options.cwd == folder
    assert options.plugins == [{"type": "local", "path": str(tmp_path / "worlds" / world)}]
    assert "WebSearch" in options.allowed_tools and "Bash" not in options.allowed_tools

    assert running.get(f"/api/runs/{run['id']}/stages/stage_nope/log").status_code == 404
    assert running.get("/api/runs/run_nope/agents").status_code == 404


def test_a_dead_session_is_replaced_by_one_that_resumes_it(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, [GRAPH, Die()], [CLAIM, FINISH, Say("Resumed.")], PASS]
    _, run = _start(running, tmp_path)
    assert _ended(running, run["id"])["status"] == "done"
    (agent,) = [
        a for a in running.get(f"/api/runs/{run['id']}/agents").json() if a["role"] == "worker"
    ]
    _, first, second, _ = script.sessions
    assert first.resume is None and second.resume == agent["session_id"]


def test_a_stage_whose_sessions_all_die_fails_the_run(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, [Die()]]
    _, run = _start(running, tmp_path)
    ended = _ended(running, run["id"])
    assert ended["status"] == "failed"
    assert "worker-1 failed 2 sessions" in ended["stages"][0]["error"]


def test_the_live_log_streams_on_the_world_events(
    live_url: str, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, [GRAPH, CLAIM, NOTE, FINISH], PASS]
    with httpx.Client(base_url=live_url, timeout=10) as client:
        world, run = _start(client, tmp_path)
        seen: list[str] = []
        with client.stream("GET", f"/api/worlds/{world}/events") as response:
            for type_, _, data in sse_events(response):
                payload = data["payload"]
                if (
                    type_ == "agent.logged"
                    and payload["kind"] == "tool"
                    and payload["agent"] == "worker-1"
                ):
                    seen.append(data["payload"]["tool"])
                if type_ == "artifact.created":
                    assert data["payload"]["type"] == "note"
                    break
        assert seen == [tool_id("query_graph"), tool_id("claim_task"), tool_id("write_note")]
        _ended(client, run["id"])


def test_the_judge_loops_on_gaps_then_projects_idea_cards_with_sources(
    live_url: str, script: Script, tmp_path: Path
) -> None:
    """Fail once with a gap task, pass on loop 2 with ranked ideas citing note paragraphs; the
    verdicts and the idea artifacts are served and stream on the world's events."""
    note = Call(tool_id("write_note"), {"title": "Trends", "body": "Chases trend.\n\nHooks win."})
    gap = {"passed": False, "reasons": ["Only one idea."], "gaps": ["Find a second idea"]}

    def passing(options: Any) -> dict:
        folder = Path(options.cwd)  # the judge runs in the stage folder: runs/<run>/<stage>
        cite = {"note": f"runs/{folder.parent.name}/{folder.name}/notes/trends.md", "paragraph": 3}
        ideas = [
            {
                "title": "Hooks",
                "body": "Open on the hook.",
                "rank": 1,
                "sources": [cite],
                "fields": {"hook": "Wait for it", "why": "Hooks win."},
            },
            {"title": "Chase", "body": "A chase.", "rank": 2, "sources": []},
        ]
        return {"passed": True, "reasons": ["Two ideas."], "items": ideas}

    script.scripts = [
        PLAN,
        [GRAPH, CLAIM, note, FINISH],
        [Answer(gap)],
        [CLAIM, FINISH],
        [Answer(passing)],
    ]
    with httpx.Client(base_url=live_url, timeout=10) as client:
        world, run = _start(client, tmp_path)
        stage = run["stages"][0]["id"]
        cite = {"note": f"runs/{run['id']}/{stage}/notes/trends.md"}
        seen: list[str] = []
        with client.stream("GET", f"/api/worlds/{world}/events") as response:
            for type_, _, data in sse_events(response):
                payload = data["payload"]
                if type_ == "verdict.created":
                    seen.append(f"verdict {payload['loop']} {payload['passed']}")
                if type_ == "artifact.created" and payload["type"] == "idea":
                    seen.append(payload["path"].rsplit("/", 1)[1])
                if len(seen) == 4:
                    break
        assert seen == ["verdict 1 False", "verdict 2 True", "idea-hooks.md", "idea-chase.md"]
        assert _ended(client, run["id"])["status"] == "done"

        base = f"/api/runs/{run['id']}/stages/{stage}"
        v1, v2 = client.get(f"{base}/verdicts").json()
        assert (v1["loop"], v1["passed"], v1["gaps"]) == (1, False, ["Find a second idea"])
        assert v2["passed"] and v2["path"].endswith("artifacts/verdict-2.md")
        tasks = client.get(f"{base}/tasks").json()
        assert [(t["title"], t["added_by"]) for t in tasks][-1] == ("Find a second idea", "judge")

        arts = {a["name"]: a for a in client.get(f"{base}/artifacts").json()}
        assert sorted(arts) == ["idea-chase.md", "idea-hooks.md", "verdict-1.md", "verdict-2.md"]
        hooks = arts["idea-hooks.md"]
        assert (hooks["type"], hooks["title"], hooks["author"]) == ("idea", "Hooks", "judge")
        assert hooks["fields"]["rank"] == 1 and hooks["fields"]["hook"] == "Wait for it"
        assert hooks["fields"]["sources"] == [f"{cite['note']}#p3"]
        assert 'paragraph 3: "Hooks win."' in hooks["body"]
        assert arts["verdict-1.md"]["fields"]["reasons"] == ["Only one idea."]
        file = tmp_path / "worlds" / world / hooks["path"]
        assert file.read_text().startswith('---\ntype: "idea"\nauthor: "judge"\n')
        assert client.get(f"/api/runs/{run['id']}/stages/nope/verdicts").status_code == 404
        assert client.get(f"/api/runs/{run['id']}/stages/nope/artifacts").status_code == 404


def test_a_fourth_failed_verdict_fails_the_run(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    fail = [Answer({"passed": False, "reasons": ["Not yet."]})]
    script.scripts = [PLAN, [GRAPH, CLAIM, FINISH]] + [fail, [CLAIM, FINISH]] * 3 + [fail]
    _, run = _start(running, tmp_path)
    ended = _ended(running, run["id"])
    assert ended["status"] == "failed"
    assert "The judge failed the stage 4 times" in ended["stages"][0]["error"]
    stage = ended["stages"][0]["id"]
    verdicts = running.get(f"/api/runs/{run['id']}/stages/{stage}/verdicts").json()
    assert [v["passed"] for v in verdicts] == [False] * 4


def test_a_send_back_reworks_the_swarm_stage_with_the_note(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    script.scripts = [PLAN, [GRAPH, CLAIM, FINISH], PASS, [CLAIM, FINISH], PASS]
    world = running.post("/api/worlds", json={"name": "Video"}).json()["id"]
    yaml = RESEARCH + "  - gate: review\n    ask: Good?\n    kind: approve\n"
    wf = running.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
    run = running.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
    url = f"/api/runs/{run['id']}"

    def at_gate() -> bool:
        return running.get(url).json()["status"] == "waiting_gate"

    _until(at_gate)
    sent = running.post(
        f"{url}/decision", json={"note": "Fewer ideas.", "send_back_to": "research"}
    )
    assert sent.status_code == 200
    _until(lambda: running.get(url).json()["send_backs"] == 1 and at_gate())

    rework = script.sessions[3]
    assert "Sent back to you with this note: Fewer ideas." in str(rework.system_prompt)
    assert "sent the stage back with this note: Fewer ideas." in script.prompts[4]
    stage = running.get(url).json()["stages"][0]["id"]
    tasks = running.get(f"{url}/stages/{stage}/tasks").json()
    assert [(t["title"], t["body"], t["status"]) for t in tasks][-1] == (
        "Rework after send-back 1",
        "Fewer ideas.",
        "done",
    )
    assert len(running.get(f"{url}/stages/{stage}/verdicts").json()) == 2
