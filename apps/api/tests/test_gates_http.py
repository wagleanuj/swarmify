"""Gates over HTTP: the inbox, decisions of every kind, send-backs, pause, resume and abort,
with real DBOS on SQLite behind the app, a fake connector and a fake notifier."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_fakes import FakeNotifier, until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_api.main import create_app
from swarmify_swarm import tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Script

GRAPH = Call(tool_id("query_graph"), {"question": "What did we reject before?"})

WEEKLY = """name: Weekly short
stages:
  - connector: research
    calls: remotion_render
  - gate: pick
    ask: Pick one idea.
    kind: pick
  - connector: script
    calls: remotion_render
    output: short.mp4
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
  - gate: publish_ok
    ask: Publish Tuesday 9am?
    kind: confirm
  - connector: publish
    calls: youtube
    input: short.mp4
    params:
      title: Five hooks that work
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
    app = create_app(tmp_path, connector=connector, notifier=notifier, retro=False)
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
    assert path.name == "decision.md" and path.parent.name == first["stages"][1]["id"]
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


IDEAS_THEN_PICK = """name: Weekly short
stages:
  - swarm: research
    goal: Find ideas.
    done_when: 2 ideas ranked
    tools: [web_search]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea.
    kind: pick
  - gate: review
    ask: Read it. Approve or send back.
    kind: approve
"""


def test_a_pick_gate_lists_the_earlier_stages_artifacts_and_records_the_chosen_one(
    app: FastAPI, script: Script, tmp_path: Path
) -> None:
    """Stories 53 and 54: the judge's idea cards reach the gate, typed; the pick names one by
    its artifact path and the decision file records it; the file itself is served for the
    skin's in-place view."""
    note = Call(tool_id("write_note"), {"title": "Trends", "body": "Hooks win."})

    def passing(options: Any) -> dict:
        folder = Path(options.cwd)
        cite = {"note": f"runs/{folder.parent.name}/{folder.name}/notes/trends.md", "paragraph": 1}
        ideas = [
            {"title": "Chase", "body": "A chase.", "rank": 2, "sources": []},
            {
                "title": "Hooks",
                "body": "Open on the hook.",
                "rank": 1,
                "sources": [cite],
                "fields": {"hook": "Wait for it", "why": "Hooks win."},
            },
        ]
        return {"passed": True, "reasons": ["Two ideas."], "items": ideas}

    plan = Answer({"tasks": [{"title": "Find ideas."}]})
    claim = Call(tool_id("claim_task"))
    finish = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
    script.scripts = [[plan], [GRAPH, claim, note, finish], [Answer(passing)]]
    with TestClient(app) as running:
        world, run = start(running, IDEAS_THEN_PICK)
        waiting_at(running, run["id"], "pick")
        research = run["stages"][0]["id"]

        before_gate = running.get(f"/api/runs/{run['id']}/artifacts", params={"before": 1})
        assert before_gate.status_code == 200
        ideas = [a for a in before_gate.json() if a["type"] == "idea"]
        assert [(a["title"], a["fields"]["rank"], a["stage"]) for a in ideas] == [
            ("Chase", 2, research),
            ("Hooks", 1, research),
        ]
        hooks = next(a for a in ideas if a["title"] == "Hooks")
        assert hooks["path"] == f"runs/{run['id']}/{research}/artifacts/idea-hooks.md"
        assert hooks["fields"]["hook"] == "Wait for it" and hooks["fields"]["why"] == "Hooks win."
        assert running.get(f"/api/runs/{run['id']}/artifacts", params={"before": 0}).json() == []
        assert running.get("/api/runs/run_nope/artifacts").status_code == 404

        raw = running.get(f"/api/worlds/{world}/files/{hooks['path']}")
        assert raw.status_code == 200 and raw.text.startswith('---\ntype: "idea"')
        assert running.get(f"/api/worlds/{world}/files/runs/../../swarmify.db").status_code == 404
        assert running.get(f"/api/worlds/{world}/files/runs/{run['id']}").status_code == 404
        assert running.get(f"/api/worlds/world_nope/files/{hooks['path']}").status_code == 404

        url = f"/api/runs/{run['id']}/decision"
        wrong = running.post(url, json={"artifact": "runs/other/x/artifacts/idea.md"})
        assert wrong.status_code == 422 and "before this gate" in wrong.json()["detail"]
        picked = running.post(url, json={"artifact": hooks["path"], "note": "Less text."})
        assert picked.status_code == 200
        (decision,) = picked.json()["decisions"]
        assert (decision["choice"], decision["artifact"]) == ("Hooks", hooks["path"])
        text = Path(decision["path"]).read_text()
        assert f'artifact: "{hooks["path"]}"' in text and "Picked Hooks." in text

        waiting_at(running, run["id"], "review")
        under_review = running.get(f"/api/runs/{run['id']}/artifacts", params={"before": 2}).json()
        assert {a["type"] for a in under_review} == {"idea", "verdict", "decision"}
        own = running.post(url, json={"artifact": hooks["path"]})
        assert own.status_code == 200, "an approve gate may name the artifact it approved"
        assert own.json()["decisions"][-1]["artifact"] == hooks["path"]


PICK_THEN_SCRIPT = """name: Weekly short
stages:
  - swarm: research
    goal: Find ideas.
    done_when: 2 ideas ranked
    tools: [web_search]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea.
    kind: pick
  - swarm: script
    goal: Write the script of the picked idea.
    done_when: A script
    tools: []
  - gate: review
    ask: Read it. Approve or send back.
    kind: approve
"""


def test_the_stages_after_a_pick_gate_get_its_choice_artifact_and_note_as_required_reading(
    app: FastAPI, script: Script
) -> None:
    """Issue 59: the planner and the workers of the next swarm stage read the gate's decision
    from their prompt, not from a stale graph; after a send-back, the newest decision."""
    ideas = [
        {"title": "Chase", "body": "A chase.", "rank": 2, "sources": []},
        {"title": "Hooks", "body": "Open on the hook.", "rank": 1, "sources": []},
    ]
    plan = Answer({"tasks": [{"title": "Do it."}]})
    claim = Call(tool_id("claim_task"))
    finish = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
    passed = Answer({"passed": True, "reasons": ["Done."]})
    script.scripts = [
        [plan],
        [GRAPH, claim, finish],
        [Answer({"passed": True, "reasons": ["Two ideas."], "items": ideas})],
        [plan],
        [GRAPH, claim, finish],
        [passed],
        [GRAPH, claim, finish],
        [passed],
    ]
    with TestClient(app) as running:
        _, run = start(running, PICK_THEN_SCRIPT)
        waiting_at(running, run["id"], "pick")
        research = run["stages"][0]["id"]
        path = f"runs/{run['id']}/{research}/artifacts/idea-%s.md"
        url = f"/api/runs/{run['id']}/decision"
        assert running.post(url, json={"artifact": path % "hooks", "note": "Less text."}).is_success

        waiting_at(running, run["id"], "review")
        planner, worker = script.prompts[3], str(script.sessions[4].system_prompt)
        for prompt in (planner, worker):
            assert "Picked Hooks." in prompt and "Note: Less text." in prompt
            assert path % "hooks" in prompt and "required reading" in prompt

        sent = running.post(url, json={"note": "Too slow.", "send_back_to": "pick"})
        assert sent.is_success
        waiting_at(running, run["id"], "pick")
        assert running.post(url, json={"artifact": path % "chase", "note": "Shorter."}).is_success

        waiting_at(running, run["id"], "review")
        rework = str(script.sessions[6].system_prompt)
        assert "Picked Chase." in rework and "Note: Shorter." in rework
        assert path % "chase" in rework and "Hooks" not in rework
