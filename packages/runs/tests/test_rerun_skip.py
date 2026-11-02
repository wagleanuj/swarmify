"""Rerun and Skip at the RunControl seam (ADR 0027): a Rerun sends the run back to a finished
stage with a note, from a gate or a pause, and the run executes it and every later stage again;
a Skip passes over a stage that has not started, unless a later stage needs what it makes or it
is the gate before a public post. Real DBOS on SQLite, fakes for the stage calls and the check."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from runs_fakes import FakeNotifier, wait_for
from swarmify_runs import Illegal, Run, RunChange, Runner, SqliteStorage, StageDef

WRITE = StageDef("write", "swarm", {"goal": "Write a script.", "output": "script.md"})
RENDER = StageDef("render", "connector", {"calls": "remotion_render", "input": "script.md"})
CONFIRM = StageDef("confirm", "gate", {"ask": "Go on?", "kind": "approve", "max_send_backs": 1})
PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
PUBLISH = StageDef("publish", "connector", {"calls": "youtube", "params": {"privacy": "public"}})
PRIVATE = StageDef("publish", "connector", {"calls": "youtube", "params": {"privacy": "private"}})


class Calls:
    """Stands in for both stage calls: records each stage that runs with the note it was given.
    A stage named `slow` holds until `release` is set; one named in `failing` fails once."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.failing: set[str] = set()

    def __call__(self, run: Run, _stage_id: str, stage: StageDef) -> None:
        if stage.name == "slow":
            self.started.set()
            self.release.wait(10)
        self.ran.append((stage.name, run.send_back_note(stage.name)))
        if stage.name in self.failing:
            self.failing.discard(stage.name)
            raise RuntimeError(f"{stage.name} was refused")


def check(_run: Run, stages: list[StageDef]) -> tuple[list[str], list[StageDef]]:
    """Stands in for the workflow check's two rules a Skip can break: an input no earlier stage
    makes, and a public post that is not right after a gate. A private post needs no gate."""
    out: list[str] = []
    made: set[str] = set()
    for before, stage in zip([None, *stages], stages, strict=False):
        wanted = stage.config.get("input")
        if wanted and wanted not in made:
            out.append(f"input '{wanted}' is not produced by an earlier stage")
        made.add(str(stage.config.get("output")))
        public = stage.config.get("params", {}).get("privacy") == "public"
        if (
            stage.config.get("calls") == "youtube"
            and public
            and (before is None or before.type != "gate")
        ):
            out.append("publish connector 'youtube' must come right after a gate")
    return out, stages


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, notifier: FakeNotifier, calls: Calls
) -> Iterator[Runner]:
    runner = Runner(
        storage, tmp_path / "dbos.sqlite", calls, tmp_path, notifier, swarm=calls,
        poll_seconds=0.05,
    )  # fmt: skip
    runner.check = check
    runner.launch()
    yield runner
    calls.release.set()
    runner.shutdown()


def fetch(runner: Runner, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def at(runner: Runner, run_id: str, status: str) -> Run:
    wait_for(lambda: fetch(runner, run_id).status == status)
    return fetch(runner, run_id)


def change(run: Run, kind: str, name: str, note: str = "") -> RunChange:
    stage = next(s for s in run.stages if s.name == name)
    return RunChange(kind, stage.id, {"note": note} if note else {})


def statuses(run: Run) -> list[str]:
    return [s.status for s in run.stages]


def test_a_rerun_resets_the_stage_and_later_ones_carries_the_note_and_runs_them_again(
    runner: Runner, calls: Calls
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, RENDER, CONFIRM], budget_usd=5.0)
    run = at(runner, run.id, "waiting_gate")
    spent = {run.stages[0].id: 1.2, run.stages[1].id: 0.4}
    runner.stage_spend = lambda _run_id: spent
    rerun = change(run, "rerun", "write", "A stronger hook.")

    proposal = runner.propose(run.id, rerun)
    assert proposal.stage_name == "write" and proposal.violations == []
    assert proposal.before == {"write": "done", "render": "done", "confirm": "waiting"}
    assert proposal.after == {"write": "pending", "render": "pending", "confirm": "pending"}
    assert proposal.cost_usd == pytest.approx(1.6) and proposal.budget_left_usd == 5.0
    assert fetch(runner, run.id).changes == [], "a proposal writes nothing"

    applied = runner.apply(run.id, rerun, proposal.revision, "operator")
    assert (applied.kind, applied.stage_name, applied.payload) == (
        "rerun", "write", {"note": "A stronger hook."},
    )  # fmt: skip
    wait_for(lambda: len(calls.ran) == 4)
    run = at(runner, run.id, "waiting_gate")
    assert calls.ran == [
        ("write", ""), ("render", ""), ("write", "A stronger hook."), ("render", ""),
    ]  # fmt: skip
    assert run.changes == [applied] and run.stages[0].notes == ["A stronger hook."]

    # Not a send-back of the gate: its one send-back is still there to use.
    runner.decide(run.id, send_back_to="write", note="Shorter.")
    wait_for(lambda: len(calls.ran) == 6)
    assert calls.ran[4] == ("write", "Shorter.")
    run = at(runner, run.id, "waiting_gate")
    # Write and render have run three times now: a Rerun estimates them at their average.
    spent[run.stages[0].id] = 3.0
    assert runner.propose(run.id, rerun).cost_usd == pytest.approx((3.0 + 0.4) / 3)


def test_a_rerun_while_the_run_is_at_work_is_refused_until_it_is_paused(
    runner: Runner, calls: Calls
) -> None:
    slow = StageDef("slow", "connector", {"calls": "remotion_render"})
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, slow, CONFIRM])
    calls.started.wait(10)
    run = at(runner, run.id, "running")
    rerun = change(run, "rerun", "write", "Again.")

    proposal = runner.propose(run.id, rerun)
    assert proposal.violations == ["the run is at work: pause first"]
    with pytest.raises(Illegal, match="pause first"):
        runner.apply(run.id, rerun, proposal.revision, "operator")

    runner.pause(run.id)
    calls.release.set()
    wait_for(lambda: statuses(fetch(runner, run.id)) == ["done", "done", "pending"])
    run = at(runner, run.id, "paused")
    runner.apply(run.id, rerun, runner.propose(run.id, rerun).revision, "operator")
    wait_for(lambda: statuses(fetch(runner, run.id)) == ["pending"] * 3)
    assert fetch(runner, run.id).status == "paused", "a paused run holds"

    runner.resume(run.id)
    at(runner, run.id, "waiting_gate")
    assert [name for name, _ in calls.ran] == ["write", "slow", "write", "slow"]
    assert calls.ran[2] == ("write", "Again.")


def test_a_rerun_of_a_stage_that_has_not_finished_is_refused(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE])
    run = at(runner, run.id, "waiting_gate")
    rerun = change(run, "rerun", "write")
    assert runner.propose(run.id, rerun).violations == ["write has not finished"]
    with pytest.raises(Illegal, match="write has not finished"):
        runner.apply(run.id, rerun, run.revision, "operator")


def test_a_skip_passes_over_a_stage_that_has_not_started(runner: Runner, calls: Calls) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE, CONFIRM, RENDER])
    run = at(runner, run.id, "waiting_gate")
    skip = change(run, "skip", "confirm")

    proposal = runner.propose(run.id, skip)
    assert (proposal.before, proposal.after) == ({"confirm": "pending"}, {"confirm": "skipped"})
    assert proposal.violations == [] and proposal.cost_usd is None
    applied = runner.apply(run.id, skip, proposal.revision, "anuj in chat")
    wait_for(lambda: fetch(runner, run.id).stages[2].status == "skipped")
    assert fetch(runner, run.id).changes == [applied]

    runner.decide(run.id, choice="idea 1")
    run = at(runner, run.id, "done")
    assert statuses(run) == ["done", "done", "skipped", "done"]
    assert [name for name, _ in calls.ran] == ["write", "render"]
    assert runner.propose(run.id, skip).violations == ["confirm is skipped"]


def test_a_skip_of_a_stage_whose_output_is_needed_or_of_a_publish_gate_is_refused(
    runner: Runner,
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE, RENDER, CONFIRM, PUBLISH])
    run = at(runner, run.id, "waiting_gate")

    for name, refused in [
        ("write", "input 'script.md' is not produced by an earlier stage"),
        ("confirm", "publish connector 'youtube' must come right after a gate"),
    ]:
        skip = change(run, "skip", name)
        proposal = runner.propose(run.id, skip)
        assert proposal.violations == [refused]
        with pytest.raises(ValueError, match=refused):
            runner.apply(run.id, skip, proposal.revision, "operator")
    assert runner.propose(run.id, change(run, "skip", "pick")).violations == ["pick has started"]
    assert fetch(runner, run.id).changes == []


def test_a_rerun_from_a_gate_closes_it_as_a_rerun(runner: Runner, notifier: FakeNotifier) -> None:
    """So a Remote Approval's message for the gate says what happened to it."""
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, CONFIRM])
    run = at(runner, run.id, "waiting_gate")
    rerun = change(run, "rerun", "write", "Again.")
    runner.apply(run.id, rerun, run.revision, "anuj in chat")
    wait_for(lambda: any(e.type == "gate.closed" for e in notifier.events))
    closed = next(e.payload for e in notifier.events if e.type == "gate.closed")
    assert (closed["stage"], closed["visit"]) == ("confirm", 0)
    assert closed["resolution"] == {
        "outcome": "rerun", "send_back_to": "write", "note": "Again.", "by": "anuj in chat",
    }  # fmt: skip


def test_a_rerun_of_a_post_reopens_its_gate_before_it_posts_again(
    runner: Runner, calls: Calls
) -> None:
    """ADR 0007: the gate's answer covered one post; posting again takes a fresh one."""
    run = runner.start("world_1", "wf_1", 1, "Short", [CONFIRM, PUBLISH, PICK])
    run = at(runner, run.id, "waiting_gate")
    runner.decide(run.id)
    wait_for(lambda: fetch(runner, run.id).stages[2].status == "waiting")
    run = fetch(runner, run.id)
    rerun = change(run, "rerun", "publish", "A better title.")

    proposal = runner.propose(run.id, rerun)
    assert proposal.before == {"confirm": "done", "publish": "done", "pick": "waiting"}
    runner.apply(run.id, rerun, proposal.revision, "operator")
    wait_for(lambda: fetch(runner, run.id).stages[0].status == "waiting")
    assert statuses(fetch(runner, run.id)) == ["waiting", "pending", "pending"]
    assert calls.ran == [("publish", "")], "it posts again only once the gate is answered"

    runner.decide(run.id)
    wait_for(lambda: len(calls.ran) == 2)
    assert calls.ran[1] == ("publish", "A better title.")


def test_a_skipped_stage_no_longer_counts_for_the_check(runner: Runner) -> None:
    """A second Skip of two gates in a row, or a Stage Edit that makes a post public once its
    gate was skipped, would leave a public post with no gate."""
    second = StageDef("again", "gate", {"ask": "Sure?", "kind": "approve"})
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE, CONFIRM, second, PUBLISH])
    run = at(runner, run.id, "waiting_gate")
    skip = change(run, "skip", "again")
    runner.apply(run.id, skip, runner.propose(run.id, skip).revision, "operator")
    wait_for(lambda: fetch(runner, run.id).stages[3].status == "skipped")
    run = fetch(runner, run.id)
    refused = "publish connector 'youtube' must come right after a gate"
    assert runner.propose(run.id, change(run, "skip", "confirm")).violations == [refused]

    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE, CONFIRM, PRIVATE])
    run = at(runner, run.id, "waiting_gate")
    skip = change(run, "skip", "confirm")
    runner.apply(run.id, skip, runner.propose(run.id, skip).revision, "operator")
    wait_for(lambda: fetch(runner, run.id).stages[2].status == "skipped")
    run = fetch(runner, run.id)
    public = RunChange("stage_edit", run.stages[3].id, PUBLISH.config)
    proposal = runner.propose(run.id, public)
    assert proposal.violations == [refused]
    with pytest.raises(ValueError, match=refused):
        runner.apply(run.id, public, proposal.revision, "operator")


def test_a_gate_answer_and_a_rerun_at_once_do_not_both_land(runner: Runner, calls: Calls) -> None:
    """The Rerun lands while the gate is being answered: the answer is refused, not dropped."""
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, CONFIRM, RENDER])
    run = at(runner, run.id, "waiting_gate")
    rerun = change(run, "rerun", "write", "Again.")

    def meanwhile(*_: object) -> None:
        runner.previews = lambda *_: None
        runner.apply(run.id, rerun, run.revision, "anuj in chat")

    runner.previews = meanwhile
    with pytest.raises(Illegal, match="moved"):
        runner.decide(run.id)
    wait_for(lambda: len(calls.ran) == 2)
    run = at(runner, run.id, "waiting_gate")
    assert run.decisions == [] and [c.kind for c in run.changes] == ["rerun"]
    assert calls.ran == [("write", ""), ("write", "Again.")]


def test_a_skip_the_run_can_no_longer_take_is_not_kept(runner: Runner, calls: Calls) -> None:
    """The run started the stage before it heard of the Skip: the stage runs and the Skip is
    not listed as applied, nor shown on the stage."""
    slow = StageDef("slow", "connector", {"calls": "remotion_render"})
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, slow, CONFIRM])
    run = at(runner, run.id, "waiting_gate")
    send, held = runner._send, []
    runner._send = lambda run_id, message: held.append((run_id, message))
    skip = change(run, "skip", "slow")
    runner.apply(run.id, skip, runner.propose(run.id, skip).revision, "operator")
    runner._send = send
    assert len(fetch(runner, run.id).changes) == 1

    runner.decide(run.id, choice="idea 1")
    calls.started.wait(10)
    send(*held[0])  # it reaches the run only now
    calls.release.set()
    wait_for(lambda: fetch(runner, run.id).changes == [])
    run = at(runner, run.id, "waiting_gate")
    assert statuses(run) == ["done", "done", "waiting"]


def test_a_rerun_of_a_failed_run_continues_as_a_fork_that_runs_only_that_stage_and_later(
    runner: Runner, calls: Calls
) -> None:
    """The upload was refused at the end: redo it without the stages before it."""
    calls.failing = {"publish"}
    run = runner.start(
        "world_1", "wf_1", 1, "Short", [WRITE, RENDER, PRIVATE, PICK], budget_usd=2.0
    )
    run = at(runner, run.id, "failed")
    runner.storage.set_spent(run.id, 2.5)  # over its Budget: the Fork starts afresh on 2 USD
    rerun = change(run, "rerun", "publish", "Try the upload again.")

    proposal = runner.propose(run.id, rerun)
    assert proposal.violations == [] and not proposal.in_place
    assert proposal.budget_left_usd == 2.0
    assert (proposal.fork_from, proposal.before) == ("publish", {"publish": "failed"})
    applied = runner.apply(run.id, rerun, proposal.revision, "operator")
    assert applied.fork is not None
    fork = at(runner, applied.fork.id, "waiting_gate")
    assert calls.ran[3:] == [("publish", "Try the upload again.")]
    assert statuses(fork) == ["done", "done", "done", "waiting"]
    assert fork.forked_by == applied and fork.stages[2].notes == ["Try the upload again."]
    assert fetch(runner, run.id).status == "failed", "a run that ended stays as it ended"


def test_a_skip_of_a_failed_run_continues_as_a_fork_with_the_stage_skipped(
    runner: Runner, calls: Calls
) -> None:
    calls.failing = {"publish"}
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, PRIVATE, RENDER])
    run = at(runner, run.id, "failed")
    skip = change(run, "skip", "publish")

    proposal = runner.propose(run.id, skip)
    assert proposal.violations == [] and (proposal.in_place, proposal.fork_from) == (
        False, "publish",
    )  # fmt: skip
    applied = runner.apply(run.id, skip, proposal.revision, "operator")
    assert applied.fork is not None
    fork = at(runner, applied.fork.id, "done")
    assert statuses(fork) == ["done", "skipped", "done"]
    assert [name for name, _ in calls.ran] == ["write", "publish", "render"]
    assert "write is done" in runner.propose(run.id, change(run, "skip", "write")).violations
    late = change(run, "rerun", "render")
    assert runner.propose(run.id, late).violations == ["render has not run"]
