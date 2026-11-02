"""The Run Assistant over HTTP: a real Agent SDK session with the scripted CLI behind it. It reads
the run through its read tools, answers with sources, proposes Run Changes through
`RunControl.propose`, can never apply one or answer a gate, and spends the world's month, not the
run's Budget."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_run_changes_http import at_gate, change, stage

from swarmify_swarm import tool_id
from swarmify_swarm.fake import Answer, Call, Die, Say, Script

READS = ("get_run", "read_run_file", "tail_stage_log", "list_run_events")
READS += ("list_workflow_runs", "query_graph")


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_a_question_is_answered_from_the_read_tools_with_its_sources(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    run = at_gate(running)
    pick = stage(run, "pick")
    notes = tmp_path / "worlds" / run["world_id"] / "runs" / run["id"] / pick["id"] / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Idea 1 won on hooks.")
    script.scripts = [
        [
            Call(tool_id("get_run"), {}),
            Call(tool_id("read_run_file"), {"path": f"{pick['id']}/notes.md"}),
            Call(tool_id("read_run_file"), {"path": "../../secrets.env"}),
            Call(tool_id("tail_stage_log"), {"stage": pick["id"]}),
            Call(tool_id("list_run_events"), {}),
            Call(tool_id("list_workflow_runs"), {}),
            Answer(
                {
                    "reply": "Idea 1 won on its hook.",
                    "sources": [
                        {"label": "from notes.md", "stage": pick["id"]},
                        {"label": "made up", "stage": "stage_nope"},
                    ],
                    "proposals": [],
                }
            ),
        ]
    ]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Why idea 1?"})
    assert asked.status_code == 200, asked.text
    reply = asked.json()
    assert reply["role"] == "assistant" and reply["text"] == "Idea 1 won on its hook."
    assert reply["sources"] == [{"label": "from notes.md", "stage": pick["id"]}]

    got, read, escaped, logged, events, versions = script.outcomes
    assert not got.is_error and f'"revision": {run["revision"]}' in got.text
    assert '"render"' in got.text and "notes.md" in got.text  # its stages, config and files
    assert read.text == "Idea 1 won on hooks."
    assert escaped.is_error and "not a file of this run" in escaped.text
    assert not logged.is_error and not events.is_error and "run.updated" in events.text
    assert '"number": 1' in versions.text and run["id"] in versions.text
    assert "Why idea 1?" in script.prompts[0]
    assert run["id"] in str(script.sessions[0].system_prompt)

    conversation = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert [(m["role"], m["text"]) for m in conversation["messages"]] == [
        ("operator", "Why idea 1?"),
        ("assistant", "Idea 1 won on its hook."),
    ]


def test_a_change_comes_back_as_the_proposal_propose_makes_and_applies_through_the_seam(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)
    edit = change(run, "render", params={"fps": 60})
    script.scripts = [
        [
            Call(tool_id("propose_change"), edit),
            Answer(
                lambda _: {
                    "reply": "Here is the edit.",
                    "sources": [],
                    "proposals": [script.last_id],
                }
            ),
        ],
        [
            Call(tool_id("propose_change"), change(run, "tag", params={"fps": 24})),
            Answer({"reply": "And this one.", "sources": []}),
        ],
    ]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Render at 60."})
    assert asked.status_code == 200, asked.text
    [card] = asked.json()["proposals"]
    proposed = running.post(f"/api/runs/{run['id']}/changes/propose", json=edit).json()
    assert card["state"] == "open" and card["proposal"] == proposed

    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**edit, "revision": card["proposal"]["revision"]}
    )
    assert applied.status_code == 201, applied.text
    marked = running.post(
        f"/api/runs/{run['id']}/assistant/proposals/{card['id']}",
        json={"state": "applied", "change_id": applied.json()["id"]},
    )
    assert marked.status_code == 200, marked.text

    # A follow-up resumes the session; its proposal goes stale once the run moves on.
    later = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "And tag?"}).json()
    assert str(script.sessions[1].resume).startswith("session-")
    [other] = later["proposals"]  # listed with none named: every proposal of the turn
    assert other["state"] == "open"
    assert running.post(f"/api/runs/{run['id']}/decision", json={"choice": "idea 1"}).is_success

    messages = running.get(f"/api/runs/{run['id']}/assistant").json()["messages"]
    states = [(p["id"], p["state"]) for m in messages for p in m["proposals"]]
    assert states == [(card["id"], "applied"), (other["id"], "stale")]
    assert messages[1]["proposals"][0]["change_id"] == applied.json()["id"]
    wrong = running.post(
        f"/api/runs/{run['id']}/assistant/proposals/{other['id']}",
        json={"state": "applied", "change_id": "change_nope"},
    )
    assert wrong.status_code == 422


def test_the_assistant_cannot_apply_a_change_or_answer_a_gate(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)
    edit = change(run, "render", params={"fps": 60})
    script.scripts = [
        [
            Call(tool_id("apply_run_change"), {**edit, "revision": run["revision"]}),
            Call(tool_id("decide_gate"), {"run_id": run["id"], "choice": "idea 1"}),
            Call(tool_id("start_run"), {}),
            Call("Bash", {"command": "curl -X POST localhost:8000/api/runs"}),
            Call("Write", {"file_path": "x.md", "content": "x"}),
            Answer({"reply": "I can only propose.", "sources": [], "proposals": []}),
        ]
    ]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Apply it."})
    assert asked.status_code == 200, asked.text
    assert len(script.outcomes) == 5
    assert all(o.is_error and "not allowed" in o.text for o in script.outcomes)
    allowed = set(script.sessions[0].allowed_tools)
    assert {tool_id(t) for t in (*READS, "propose_change")} <= allowed
    assert not any("apply" in t or "decide" in t or "gate" in t for t in allowed)
    after = running.get(f"/api/runs/{run['id']}").json()
    assert after["status"] == "waiting_gate" and after["changes"] == []


def test_asking_costs_the_worlds_month_never_the_runs_budget(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)
    world = run["world_id"]
    before = running.get(f"/api/worlds/{world}/spend").json()["spent_usd"]
    script.cost = 0.02
    script.scripts = [[Answer({"reply": "Nothing spent yet.", "sources": [], "proposals": []})]]
    for _ in range(2):
        asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Cost?"})
        assert asked.is_success
    spend = running.get(f"/api/worlds/{world}/spend").json()["spent_usd"]
    assert spend == pytest.approx(before + 0.04)
    assert running.get(f"/api/runs/{run['id']}").json()["spent_usd"] == run["spent_usd"]
    conversation = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert conversation["spent_usd"] == pytest.approx(0.04)


def test_the_assistant_runs_on_the_worlds_planner_model_and_a_bad_turn_is_a_502(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)
    running.patch(f"/api/worlds/{run['world_id']}", json={"planner_model": "claude-sonnet-5-5"})
    script.scripts = [[Die()], [Say("no json")], [Answer({"reply": "Fine."})]]
    for _ in range(2):
        failed = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "x"})
        assert failed.status_code == 502
    assert running.post(f"/api/runs/{run['id']}/assistant", json={"message": "x"}).is_success
    assert script.sessions[0].model == "claude-sonnet-5-5"
    conversation = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert [m["role"] for m in conversation["messages"]] == ["operator", "assistant"]
    assert running.post("/api/runs/run_nope/assistant", json={"message": "x"}).status_code == 404
    assert running.get("/api/runs/run_nope/assistant").status_code == 404
    assert running.post(f"/api/runs/{run['id']}/assistant", json={"message": ""}).status_code == 422
