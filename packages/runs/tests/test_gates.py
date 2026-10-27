"""Gates at the runner's seam: real DBOS on SQLite, a fake connector and a fake notifier."""

import threading
from contextlib import closing
from pathlib import Path

import pytest
from dbos import DBOS

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_runs import Illegal, Run, Runner, SqliteStorage, StageDef
from swarmify_runs.control import RunControl

RESEARCH = StageDef("research", "connector", {"calls": "web_search"})
PICK = StageDef("pick", "gate", {"ask": "Pick one idea.", "kind": "pick"})
SCRIPT = StageDef("script", "connector", {"calls": "writer"})
REVIEW = StageDef("review", "gate", {"ask": "Watch it. Approve or send back.", "kind": "approve"})
CONFIRM = StageDef("confirm", "gate", {"ask": "Publish Tuesday 9am?", "kind": "confirm"})
PUBLISH = StageDef("publish", "connector", {"calls": "youtube"})
WEEKLY = [RESEARCH, PICK, SCRIPT, REVIEW, CONFIRM, PUBLISH]


def fetch(runner: RunControl, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def at(runner: RunControl, run_id: str, status: str, stage: str | None = None) -> None:
    def check() -> bool:
        run = fetch(runner, run_id)
        waiting = next((s.name for s in run.stages if s.status == "waiting"), None)
        return run.status == status and (stage is None or waiting == stage)

    wait_for(check)


def test_each_gate_kind_pauses_the_run_lists_in_the_inbox_and_records_a_decision_file(
    runner: Runner, tmp_path: Path
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Weekly short", WEEKLY)
    at(runner, run.id, "waiting_gate", "pick")
    (gate,) = runner.gates()
    assert (gate.run_id, gate.stage_name, gate.kind, gate.ask) == (
        run.id,
        "pick",
        "pick",
        "Pick one idea.",
    )
    assert gate.earlier_stages == ["research"] and gate.send_backs_left == 2
    assert gate.opened_at and gate.run_name == "Weekly short" and gate.world_id == "world_1"

    with pytest.raises(ValueError, match="needs a choice"):
        runner.decide(run.id, note="no pick")
    picked = runner.decide(run.id, choice="idea 2", note="Less text on screen.")
    (decision,) = picked.decisions
    assert (decision.stage_name, decision.kind, decision.choice, decision.note) == (
        "pick",
        "pick",
        "idea 2",
        "Less text on screen.",
    )
    path = Path(decision.path)
    assert path == tmp_path / "worlds/world_1/runs" / run.id / run.stages[1].id / "decision.md"
    text = path.read_text()
    assert text.startswith('---\ntype: "decision"\n')
    assert f'run: "{run.id}"\n' in text and 'stage: "pick"\n' in text
    assert 'kind: "pick"\nchoice: "idea 2"\n' in text
    body = "---\n\n# Decision at pick\n\nPicked idea 2.\n\nNote: Less text on screen.\n"
    assert text.endswith(body)

    at(runner, run.id, "waiting_gate", "review")
    approved = runner.decide(run.id)
    assert approved.decisions[-1].kind == "approve" and approved.decisions[-1].choice is None
    at(runner, run.id, "waiting_gate", "confirm")
    with pytest.raises(Illegal, match="confirm gate cannot send back"):
        runner.decide(run.id, send_back_to="script")
    runner.decide(run.id, note="Go.")
    at(runner, run.id, "done")
    done = fetch(runner, run.id)
    assert [d.kind for d in done.decisions] == ["pick", "approve", "confirm"]
    assert Path(done.decisions[2].path).read_text().endswith("Confirmed.\n\nNote: Go.\n")
    assert runner.gates() == []


def test_send_back_reruns_the_named_stage_with_the_note_and_the_third_is_refused(
    runner: Runner, connector: FakeConnector
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Review loop", [RESEARCH, SCRIPT, REVIEW, PUBLISH])
    at(runner, run.id, "waiting_gate", "review")
    with pytest.raises(Illegal, match="no earlier stage named 'publish'"):
        runner.decide(run.id, send_back_to="publish")

    runner.decide(run.id, note="Too much text on screen.", send_back_to="script")
    wait_for(lambda: fetch(runner, run.id).send_backs == 1)
    at(runner, run.id, "waiting_gate", "review")
    run = fetch(runner, run.id)
    assert [name for _, name in connector.calls] == ["research", "script", "script"]
    assert run.stages[1].notes == ["Too much text on screen."]
    assert run.stages[0].notes == [] and run.stages[2].status == "waiting"
    assert (
        Path(run.decisions[0].path)
        .read_text()
        .endswith("Sent back to script.\n\nNote: Too much text on screen.\n")
    )
    assert run.decisions[0].send_back_to == "script"
    (gate,) = runner.gates()
    assert gate.send_backs_left == 1

    runner.decide(run.id, note="Shorter reveal.", send_back_to="script")
    wait_for(lambda: fetch(runner, run.id).send_backs == 2)
    at(runner, run.id, "waiting_gate", "review")
    run = fetch(runner, run.id)
    assert run.stages[1].notes == ["Too much text on screen.", "Shorter reveal."]
    assert Path(run.decisions[1].path).name == "decision-2.md"
    assert runner.gates()[0].send_backs_left == 0

    with pytest.raises(Illegal, match="already sent back 2 times"):
        runner.decide(run.id, send_back_to="script")
    assert len(fetch(runner, run.id).decisions) == 2, "a refused send-back leaves no record"
    runner.decide(run.id, note="Fine.")
    at(runner, run.id, "done")
    calls = [name for _, name in connector.calls]
    assert calls == ["research", "script", "script", "script", "publish"]


def test_gate_opened_and_run_ended_reach_the_notifier_and_the_world_stream(
    runner: Runner, notifier: FakeNotifier, storage: SqliteStorage
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Weekly short", [RESEARCH, PICK])
    at(runner, run.id, "waiting_gate", "pick")
    runner.decide(run.id, choice="idea 1")
    at(runner, run.id, "done")
    wait_for(lambda: len(notifier.events) == 2)
    opened, ended = notifier.events
    assert opened.type == "gate.opened" and opened.world_id == "world_1" and opened.run_id == run.id
    assert opened.payload == {
        "run_id": run.id,
        "name": "Weekly short",
        "stage_index": 1,
        "stage": "pick",
        "kind": "pick",
        "ask": "Pick one idea.",
        "publish": None,
    }
    assert ended.type == "run.ended"
    assert ended.payload == {"run_id": run.id, "name": "Weekly short", "status": "done"}

    with closing(connect(storage.path)) as conn:
        types = [e.type for e in read_after(conn, "world_1", 0)]
    assert types.count("gate.opened") == 1 and types.count("run.ended") == 1
    assert types.index("gate.opened") < types.index("run.ended")


def test_a_failed_run_ends_too(runner: Runner, notifier: FakeNotifier) -> None:
    run = runner.start("world_1", "wf_1", 1, "Broken", [StageDef("explode", "connector")])
    at(runner, run.id, "failed")
    wait_for(lambda: len(notifier.events) == 1)
    assert notifier.events[0].payload["status"] == "failed"


def test_pause_holds_the_run_and_resume_moves_it_on(
    runner: Runner, connector: FakeConnector
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Pausable", [RESEARCH, PICK, SCRIPT, PUBLISH])
    at(runner, run.id, "waiting_gate", "pick")
    runner.pause(run.id)
    at(runner, run.id, "paused")
    with pytest.raises(Illegal, match="nothing to pause"):
        runner.pause(run.id)
    assert fetch(runner, run.id).stages[1].status == "waiting"
    assert [g.stage_name for g in runner.gates()] == ["pick"], "a paused gate still wants an answer"

    runner.decide(run.id, choice="idea 3")
    wait_for(lambda: fetch(runner, run.id).stages[1].status == "done")
    assert fetch(runner, run.id).status == "paused"
    assert [name for _, name in connector.calls] == ["research"], "paused: nothing moves"

    runner.resume(run.id)
    at(runner, run.id, "done")
    assert [name for _, name in connector.calls] == ["research", "script", "publish"]
    with pytest.raises(Illegal, match="run is done"):
        runner.resume(run.id)


def test_pause_sent_mid_stage_lands_when_the_stage_finishes(
    runner: Runner, connector: FakeConnector, storage: SqliteStorage
) -> None:
    gate_open = threading.Event()
    original = connector.__call__

    def slow(run: Run, stage_id: str, stage: StageDef) -> None:
        original(run, stage_id, stage)
        if stage.name == "research":
            gate_open.wait(5)

    runner.connector = slow
    run = runner.start("world_1", "wf_1", 1, "Slow", [RESEARCH, SCRIPT, PICK])
    wait_for(lambda: len(connector.calls) == 1)
    runner.pause(run.id)  # accepted while the stage runs, applied after it
    assert fetch(runner, run.id).status == "running"
    gate_open.set()
    at(runner, run.id, "paused")
    assert [s.status for s in fetch(runner, run.id).stages] == ["done", "pending", "pending"]
    with closing(connect(storage.path)) as conn:
        shown = [e.payload for e in read_after(conn, "world_1", 0) if e.type == "run.updated"]
    paused = [[s["status"] for s in p["stages"]] for p in shown if p["status"] == "paused"]
    assert paused == [["done", "pending", "pending"]]  # never paused with the stage still running
    runner.resume(run.id)
    at(runner, run.id, "waiting_gate", "pick")


def test_abort_ends_the_run_and_cascades_to_its_children(
    runner: Runner, notifier: FakeNotifier
) -> None:
    parent = runner.start("world_1", "wf_1", 1, "Parent", [RESEARCH, PICK, PUBLISH])
    at(runner, parent.id, "waiting_gate", "pick")
    child = runner.start("world_2", "wf_2", 1, "Child", [PICK], parent_run_id=parent.id)
    at(runner, child.id, "waiting_gate", "pick")
    grandchild = runner.start("world_2", "wf_3", 1, "Grandchild", [PICK], parent_run_id=child.id)
    at(runner, grandchild.id, "waiting_gate", "pick")
    assert fetch(runner, child.id).parent_run_id == parent.id
    assert runner.gates()[0].parent_run_id == child.id

    runner.abort(parent.id)
    for run_id in (parent.id, child.id, grandchild.id):
        at(runner, run_id, "aborted")
    aborted = fetch(runner, parent.id)
    assert [s.status for s in aborted.stages] == ["done", "failed", "pending"]
    assert runner.gates() == []
    with pytest.raises(Illegal, match="run is aborted"):
        runner.decide(parent.id, choice="x")
    wait_for(lambda: sum(e.type == "run.ended" for e in notifier.events) == 3)
    assert {e.payload["status"] for e in notifier.events if e.type == "run.ended"} == {"aborted"}


def test_abort_reaches_a_live_grandchild_past_a_finished_child(runner: Runner) -> None:
    parent = runner.start("world_1", "wf_1", 1, "Parent", [PICK])
    child = runner.start("world_2", "wf_2", 1, "Child", [CONFIRM], parent_run_id=parent.id)
    grandchild = runner.start("world_2", "wf_3", 1, "Grandchild", [PICK], parent_run_id=child.id)
    for run_id in (parent.id, child.id, grandchild.id):
        at(runner, run_id, "waiting_gate")
    runner.decide(child.id)
    at(runner, child.id, "done")

    runner.abort(parent.id)
    at(runner, parent.id, "aborted")
    at(runner, grandchild.id, "aborted")
    assert fetch(runner, child.id).status == "done"


def test_a_queued_run_aborts_at_once_without_a_slot(runner: Runner, notifier: FakeNotifier) -> None:
    held = [runner.start("world_1", "wf_1", 1, f"Held {i}", [PICK]) for i in range(2)]
    for run in held:
        at(runner, run.id, "waiting_gate", "pick")
    queued = runner.start("world_1", "wf_1", 1, "Queued", [RESEARCH, PICK])
    assert fetch(runner, queued.id).status == "queued"

    aborted = runner.abort(queued.id)
    assert aborted.status == "aborted"
    assert [s.status for s in aborted.stages] == ["pending", "pending"]
    assert [e.payload["name"] for e in notifier.events if e.type == "run.ended"] == ["Queued"]
    runner.decide(held[0].id, choice="a")
    at(runner, held[0].id, "done")
    assert fetch(runner, queued.id).status == "aborted", "the cancelled workflow never ran"


def test_unknown_runs_and_wrong_moments_are_refused(runner: Runner) -> None:
    with pytest.raises(KeyError):
        runner.decide("run_nope", choice="x")
    run = runner.start("world_1", "wf_1", 1, "One", [RESEARCH, PICK])
    with pytest.raises(Illegal, match="not waiting at a gate"):
        runner.decide(run.id, choice="early")
    with pytest.raises(Illegal, match="run is not paused"):
        runner.resume(run.id)


def test_a_stale_message_at_a_gate_is_ignored_and_the_run_still_finishes(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Stale", [RESEARCH, PICK, SCRIPT, REVIEW, PUBLISH])
    at(runner, run.id, "waiting_gate", "pick")
    runner.decide(run.id, choice="idea 1")
    at(runner, run.id, "waiting_gate", "review")
    runner.client.send(run.id, {"action": "resume"}, topic="control")
    runner.client.send(run.id, {"action": "rewind"}, topic="control")  # unknown: not an abort
    runner.client.send(run.id, {"action": "decide", "index": 1}, topic="control")  # the old gate
    runner.decide(run.id)
    at(runner, run.id, "done")


def test_a_second_answer_to_the_same_gate_is_refused(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people answering at once both see the gate open; only the first answer counts."""
    run = runner.start("world_1", "wf_1", 1, "Double", [RESEARCH, PICK, PUBLISH])
    at(runner, run.id, "waiting_gate", "pick")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "_send", lambda _run_id, message: sent.append(message))
    runner.decide(run.id, choice="idea 1")  # the message is in flight: storage still says waiting
    with pytest.raises(Illegal, match="already decided"):
        runner.decide(run.id, choice="idea 2")
    (decision,) = fetch(runner, run.id).decisions
    assert decision.choice == "idea 1" and "idea 1" in Path(decision.path).read_text()
    monkeypatch.undo()
    runner._send(run.id, sent[0])  # pyright: ignore[reportPrivateUsage]
    at(runner, run.id, "done")


def test_control_works_from_another_process_without_launching_dbos(
    runner: Runner, storage: SqliteStorage, tmp_path: Path, notifier: FakeNotifier
) -> None:
    """The MCP server acts on runs through a RunControl on the same system database."""
    run = runner.start("world_1", "wf_1", 1, "Remote", [RESEARCH, PICK, PUBLISH])
    at(runner, run.id, "waiting_gate", "pick")
    remote = RunControl(storage, tmp_path / "dbos.sqlite", tmp_path, notifier)
    try:
        assert [g.run_id for g in remote.gates()] == [run.id]
        remote.decide(run.id, choice="idea 1")
        at(remote, run.id, "done")
    finally:
        remote.close()


def test_a_notifier_that_raises_does_not_fail_the_run(runner: Runner) -> None:
    class Boom:
        def notify(self, event: object) -> None:
            raise RuntimeError("slack is down")

    runner.notifier = Boom()
    run = runner.start("world_1", "wf_1", 1, "Loud", [CONFIRM])
    at(runner, run.id, "waiting_gate", "confirm")
    runner.decide(run.id)
    at(runner, run.id, "done")


def test_a_decision_that_cannot_reach_the_run_is_undone_and_can_be_retried(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Retry", [CONFIRM])
    at(runner, run.id, "waiting_gate", "confirm")
    with monkeypatch.context() as m:
        m.setattr(runner, "_send", lambda *_: (_ for _ in ()).throw(OSError("db locked")))
        with pytest.raises(OSError):
            runner.decide(run.id)
    assert fetch(runner, run.id).decisions == []
    assert not list(Path(runner.data_dir).rglob("decision*.md"))
    assert len(runner.decide(run.id).decisions) == 1
    at(runner, run.id, "done")


def test_a_run_already_ended_when_its_workflow_loads_returns_at_once(
    runner: Runner, storage: SqliteStorage
) -> None:
    ended = Run("run_gone", "world_1", "wf_1", 1, "Gone", "aborted", "t", "t", [])
    runner._enqueue(storage.add_run(ended, [PICK]))  # pyright: ignore[reportPrivateUsage]
    wait_for(lambda: getattr(DBOS.get_workflow_status("run_gone"), "status", None) == "SUCCESS")
