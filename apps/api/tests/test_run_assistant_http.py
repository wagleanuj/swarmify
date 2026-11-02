"""The Run Assistant over HTTP: a real Agent SDK session with the scripted CLI behind it. It reads
the run through its read tools, answers with sources, proposes Run Changes through
`RunControl.propose`, can never apply one or answer a gate, and spends the world's month, not the
run's Budget."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import sign_in_youtube
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_publish_http import SCRIPT, setup, weekly
from test_publish_http import at_gate as publish_gate
from test_rerun_skip_http import at_check, step
from test_run_changes_http import at_gate, change, failed_at_the_end, restructure, shape, stage

from swarmify_connectors import FakeTransport
from swarmify_memory import FakeGraphIndex
from swarmify_memory.ports import graph_file
from swarmify_swarm import SqliteStorage, tool_id
from swarmify_swarm.fake import Answer, Call, Die, Say, Script

READS = ("get_run", "read_run_file", "tail_stage_log", "list_run_events")
READS += ("list_workflow_runs", "query_graph")


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_a_question_is_answered_from_the_read_tools_with_its_sources(
    running: TestClient, script: Script, tmp_path: Path, graph_index: FakeGraphIndex
) -> None:
    run = at_gate(running)
    pick = stage(run, "pick")
    board = SqliteStorage(tmp_path / "swarmify.db")
    board.agent(run["world_id"], run["id"], pick["id"], "scout", "worker")
    board.log(pick["id"], "scout", "tool", "read the three hooks", tool="Read")
    graph = graph_file(tmp_path / "worlds" / run["world_id"])
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text("{}")
    notes = tmp_path / "worlds" / run["world_id"] / "runs" / run["id"] / pick["id"] / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Idea 1 won on hooks.")
    script.scripts = [
        [
            Call(tool_id("get_run"), {}),
            Call(tool_id("read_run_file"), {"path": f"{pick['id']}/notes.md"}),
            Call(tool_id("read_run_file"), {"path": "../../secrets.env"}),
            Call(tool_id("tail_stage_log"), {"stage": pick["id"]}),
            Call(tool_id("tail_stage_log"), {"stage": pick["id"], "lines": -5}),
            Call(tool_id("list_run_events"), {}),
            Call(tool_id("list_workflow_runs"), {}),
            Call(tool_id("query_graph"), {"question": "Which hooks win?"}),
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

    got, read, escaped, logged, clamped, events, versions, recalled = script.outcomes
    assert not got.is_error and f'"revision": {run["revision"]}' in got.text
    assert '"render"' in got.text and "notes.md" in got.text  # its stages, config and files
    assert read.text == "Idea 1 won on hooks."
    assert escaped.is_error and "not a file of this run" in escaped.text
    assert not logged.is_error and "read the three hooks" in logged.text
    assert "read the three hooks" in clamped.text  # the tail, however few lines asked
    assert not events.is_error and "run.updated" in events.text
    assert recalled.text == graph_index.answer
    assert graph_index.calls[-1][2] == {"question": "Which hooks win?"}
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
    elsewhere = running.post(  # a real change, but not the one this card proposed
        f"/api/runs/{run['id']}/assistant/proposals/{other['id']}",
        json={"state": "applied", "change_id": applied.json()["id"]},
    )
    assert elsewhere.status_code == 422
    again = running.post(
        f"/api/runs/{run['id']}/assistant/proposals/{card['id']}", json={"state": "dismissed"}
    )
    assert again.status_code == 409


def test_a_rerun_a_skip_and_a_restructure_come_back_as_proposals_that_apply_through_the_seam(
    running: TestClient, script: Script
) -> None:
    run = at_check(running)
    review = {"name": "review", "type": "gate", "ask": "Ready to upload?"}
    rest = [review, shape(run, "confirm"), shape(run, "publish")]
    asks = {
        "Redo the render, stronger hook.": step(run, "rerun", "render", note="Stronger hook."),
        "Skip the tagging.": step(run, "skip", "tag"),
        "Add a review gate before upload.": restructure(run, "confirm", rest),
    }
    script.scripts = [
        [Call(tool_id("propose_change"), c), Answer({"reply": "Here it is.", "sources": []})]
        for c in asks.values()
    ]
    for n, (message, proposed) in enumerate(asks.items()):
        asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": message})
        assert asked.status_code == 200, asked.text
        [card] = asked.json()["proposals"]
        made = running.post(f"/api/runs/{run['id']}/changes/propose", json=proposed).json()
        assert card["proposal"] == made and made["violations"] == []
        assert card["proposal"]["change"]["kind"] == proposed["kind"]
        told = str(script.sessions[n].system_prompt)
        assert all(k in told for k in ("rerun", "skip", "restructure", '{"note"', '{"stages"'))

    # The Rerun applies with its note, at the revision it was proposed against.
    [rerun] = running.get(f"/api/runs/{run['id']}/assistant").json()["messages"][1]["proposals"]
    body = {**rerun["proposal"]["change"], "revision": rerun["proposal"]["revision"]}
    applied = running.post(f"/api/runs/{run['id']}/changes", json=body)
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"] == {"note": "Stronger hook."}


def test_asked_to_redo_a_stage_of_a_failed_run_it_proposes_a_rerun_that_continues_as_a_fork(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    run = failed_at_the_end(running, transport)
    rerun = step(run, "rerun", "render_video", note="Upload it again.")
    script.scripts = [
        [Call(tool_id("propose_change"), rerun), Answer({"reply": "Re-run it.", "sources": []})]
    ]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Redo the upload."})
    assert asked.status_code == 200, asked.text
    [card] = asked.json()["proposals"]
    made = running.post(f"/api/runs/{run['id']}/changes/propose", json=rerun).json()
    assert card["proposal"] == made and made["violations"] == []
    assert (made["in_place"], made["fork_from"]) == (False, "render_video")
    assert "has ended" in str(script.sessions[0].system_prompt)

    body = {**rerun, "revision": made["revision"]}
    applied = running.post(f"/api/runs/{run['id']}/changes", json=body)
    assert applied.status_code == 201 and applied.json()["fork"]["id"], applied.text


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
    first = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert (first["average_usd"], first["month_left_usd"]) == (None, None)
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

    # What a question costs is the world's real average, against what its month has left.
    script.cost = 0.19
    assert running.post(f"/api/runs/{run['id']}/assistant", json={"message": "?"}).is_success
    running.patch(f"/api/worlds/{world}", json={"monthly_budget_usd": 10})
    conversation = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert conversation["average_usd"] == pytest.approx((0.02 + 0.02 + 0.19) / 3)
    assert conversation["month_left_usd"] == pytest.approx(10 - before - 0.04 - 0.19)


def test_the_assistant_runs_on_the_worlds_planner_model_and_a_bad_turn_is_a_502(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)
    world = run["world_id"]
    running.patch(f"/api/worlds/{world}", json={"planner_model": "claude-sonnet-5-5"})
    before = running.get(f"/api/worlds/{world}/spend").json()["spent_usd"]
    script.cost = 0.02
    script.scripts = [[Die()], [Say("no json")], [Answer({"reply": "Fine."})]]
    for _ in range(2):
        failed = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "x"})
        assert failed.status_code == 502
    assert running.post(f"/api/runs/{run['id']}/assistant", json={"message": "x"}).is_success
    assert script.sessions[0].model == "claude-sonnet-5-5"
    conversation = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert [m["role"] for m in conversation["messages"]] == ["operator", "assistant"]
    # The failed "no json" turn was still billed: it counts, beside the answered one.
    spend = running.get(f"/api/worlds/{world}/spend").json()["spent_usd"]
    assert spend == pytest.approx(before + 0.04)
    assert conversation["spent_usd"] == pytest.approx(0.04)
    assert running.post("/api/runs/run_nope/assistant", json={"message": "x"}).status_code == 404
    assert running.get("/api/runs/run_nope/assistant").status_code == 404
    assert running.post(f"/api/runs/{run['id']}/assistant", json={"message": ""}).status_code == 422


def test_at_a_publish_gate_the_assistant_reads_the_form_and_fills_its_fields(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    sign_in_youtube(tmp_path)
    world, workflow = setup(running, weekly(extra="\n      metadata: script.md"))
    run = publish_gate(running, tmp_path, world, workflow)
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / "01-render"
    (folder / "script.md").write_text(SCRIPT)
    fill = {"hashtags": "#Owls", "visibility": "private", "nope": "x"}
    script.scripts = [[Answer({"reply": "Cut to one.", "sources": [], "fields": fill})]]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Cut them."})
    assert asked.status_code == 200, asked.text
    # Only the fields the gate's form edits: the rest is a Run Change or not the gate's.
    assert asked.json()["fields"] == {"hashtags": "#Owls"}
    # Never kept: the form is the operator's to confirm.
    kept = running.get(f"/api/runs/{run['id']}/assistant").json()["messages"][-1]
    assert kept["fields"] == {}
    told = str(script.sessions[0].system_prompt)
    assert "hashtags" in told and "#Owls #AnimalFacts" in told and "artifact_edit" in told


def test_away_from_a_publish_gate_the_assistant_fills_no_fields(
    running: TestClient, script: Script
) -> None:
    run = at_gate(running)  # a pick gate, nothing to publish after it
    script.scripts = [[Answer({"reply": "No form here.", "fields": {"title": "x"}})]]
    asked = running.post(f"/api/runs/{run['id']}/assistant", json={"message": "Retitle it."})
    assert asked.status_code == 200, asked.text
    assert asked.json()["fields"] == {}
    assert "fields" not in str(script.sessions[0].system_prompt)
