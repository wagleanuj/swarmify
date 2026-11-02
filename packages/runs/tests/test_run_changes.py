"""Run Changes at the RunControl seam (ADR 0027), Stage Edit first: propose writes nothing and
shows the check, apply re-checks, writes the pending stage's config and records who changed it.
Real DBOS on SQLite, fakes for the stage calls and for the workflow check."""

import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from runs_fakes import FakeNotifier, wait_for
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_runs import (
    Illegal,
    PublishField,
    PublishPreview,
    Run,
    RunChange,
    RunLink,
    Runner,
    SqliteStorage,
    StageDef,
)

PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
WRITE = StageDef("write", "swarm", {"goal": "Write a script.", "done_when": "It reads well."})
CONFIRM = StageDef("confirm", "gate", {"ask": "Go on?", "kind": "approve"})
RENDER = StageDef("render", "connector", {"calls": "remotion_render", "params": {"fps": 30}})


class Calls:
    """Stands in for both stage calls: records each stage's config as it runs, and which ran.
    A stage named `slow` holds until `release` is set; one whose config says `fail` fails."""

    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.ran: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, _run: Run, _stage_id: str, stage: StageDef) -> None:
        if stage.name == "slow":
            self.started.set()
            self.release.wait(10)
        self.ran.append(stage.name)
        if stage.config.get("fail"):
            raise RuntimeError(f"{stage.name} said no")
        self.configs[stage.name] = stage.config


class Check:
    """Stands in for the workflow check: refuses a connector stage that calls `nobody`, and
    keeps the stage list it was asked about."""

    def __init__(self) -> None:
        self.seen: list[list[StageDef]] = []

    def __call__(self, _run: Run, stages: list[StageDef]) -> tuple[list[str], list[StageDef]]:
        self.seen.append(stages)
        return [
            f"no connector named '{s.config['calls']}'"
            for s in stages
            if s.config.get("calls") == "nobody"
        ], stages


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def check() -> Check:
    return Check()


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, notifier: FakeNotifier, calls: Calls, check: Check
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


def edit(run: Run, name: str, config: dict[str, Any]) -> RunChange:
    stage = next(s for s in run.stages if s.name == name)
    return RunChange("stage_edit", stage.id, config)


def restructure(run: Run, name: str, stages: list[StageDef]) -> RunChange:
    stage = next(s for s in run.stages if s.name == name)
    shape = [{"name": d.name, "type": d.type, **d.config} for d in stages]
    return RunChange("restructure", stage.id, {"stages": shape})


def events(storage: SqliteStorage, kind: str) -> list[dict[str, Any]]:
    with closing(connect(storage.path)) as conn:
        return [e.payload for e in read_after(conn, "world_1", 0) if e.type == kind]


def test_a_stage_edit_of_a_pending_stage_applies_and_the_stage_runs_with_it(
    runner: Runner, storage: SqliteStorage, calls: Calls, check: Check
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, WRITE, RENDER])
    run = at(runner, run.id, "waiting_gate")
    goal = {**WRITE.config, "goal": "Write a script with a stronger hook."}

    proposal = runner.propose(run.id, edit(run, "write", goal))
    assert (proposal.before, proposal.after) == (WRITE.config, goal)
    assert proposal.violations == [] and proposal.in_place
    assert proposal.revision == run.revision
    assert [s.config for s in check.seen[-1]] == [PICK.config, goal, RENDER.config]
    assert fetch(runner, run.id).changes == [], "a proposal writes nothing"

    applied = runner.apply(run.id, edit(run, "write", goal), proposal.revision, "operator")
    assert (applied.stage_name, applied.by, applied.before) == ("write", "operator", WRITE.config)
    after = fetch(runner, run.id)
    assert after.revision > run.revision
    assert after.changes == [applied]
    assert next(s for s in after.stages if s.name == "write").config == goal
    fps = {**RENDER.config, "params": {"fps": 60}}
    runner.apply(run.id, edit(run, "render", fps), after.revision, "operator")

    runner.decide(run.id, choice="idea 1")
    at(runner, run.id, "done")
    assert calls.configs == {"write": goal, "render": fps}
    changed = events(storage, "run.changed")
    assert [(c["stage_name"], c["payload"]) for c in changed] == [("write", goal), ("render", fps)]
    assert changed[0]["run_id"] == run.id and changed[0]["by"] == "operator"


def test_a_stage_edit_of_the_running_stage_or_a_finished_one_is_refused(
    runner: Runner, calls: Calls
) -> None:
    slow = StageDef("slow", "connector", {"calls": "remotion_render"})
    run = runner.start("world_1", "wf_1", 1, "Short", [slow, RENDER])
    calls.started.wait(10)
    run = at(runner, run.id, "running")
    change = edit(run, "slow", {"calls": "remotion_build"})

    proposal = runner.propose(run.id, change)
    assert proposal.violations == ["slow is running: pause first or add a task"]
    with pytest.raises(Illegal, match="pause first or add a task"):
        runner.apply(run.id, change, proposal.revision, "operator")

    calls.release.set()
    run = at(runner, run.id, "done")
    assert not runner.propose(run.id, change).in_place, "a run that ended changes as a Fork"
    assert calls.configs["slow"] == slow.config


def test_a_stage_edit_that_breaks_a_workflow_rule_is_refused_at_propose_and_at_apply(
    runner: Runner,
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, RENDER])
    run = at(runner, run.id, "waiting_gate")
    change = edit(run, "render", {"calls": "nobody"})

    proposal = runner.propose(run.id, change)
    assert proposal.violations == ["no connector named 'nobody'"]
    with pytest.raises(ValueError, match="no connector named 'nobody'"):
        runner.apply(run.id, change, proposal.revision, "operator")
    assert fetch(runner, run.id).changes == []


def test_a_stale_revision_is_refused_naming_what_moved(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [PICK, CONFIRM, RENDER])
    run = at(runner, run.id, "waiting_gate")
    fps = edit(run, "render", {**RENDER.config, "params": {"fps": 60}})
    cut = edit(run, "render", {**RENDER.config, "params": {"fps": 24}})
    seen = runner.propose(run.id, fps).revision

    runner.apply(run.id, cut, seen, "anuj in chat")
    with pytest.raises(Illegal, match="moved on since this was proposed: render was edited by"):
        runner.apply(run.id, fps, seen, "operator")

    seen = fetch(runner, run.id).revision
    runner.decide(run.id, choice="idea 1")
    wait_for(lambda: fetch(runner, run.id).stages[1].status == "waiting")
    with pytest.raises(Illegal, match="moved on since this was proposed: it is now at confirm"):
        runner.apply(run.id, fps, seen, "operator")
    assert [c.by for c in fetch(runner, run.id).changes] == ["anuj in chat"]


def test_a_stage_edit_of_the_publish_a_waiting_gate_is_confirming_is_refused(
    runner: Runner, calls: Calls
) -> None:
    def previews(_run: Run, stage: StageDef, _edits: dict[str, Any]) -> PublishPreview | None:
        title = str(stage.config["params"]["title"])
        return PublishPreview("youtube", [PublishField("title", "Title", title)])

    runner.previews = previews
    publish = StageDef("publish", "connector", {"calls": "youtube", "params": {"title": "Hooks"}})
    run = runner.start("world_1", "wf_1", 1, "Short", [CONFIRM, publish])
    run = at(runner, run.id, "waiting_gate")
    change = edit(run, "publish", {"calls": "youtube", "params": {"title": "Something else"}})

    proposal = runner.propose(run.id, change)
    refused = "confirm is confirming publish: answer or send back the gate first"
    assert proposal.violations == [refused]
    with pytest.raises(Illegal, match=refused):
        runner.apply(run.id, change, proposal.revision, "anuj in chat")

    runner.decide(run.id)
    at(runner, run.id, "done")
    assert calls.configs["publish"] == publish.config, "what posts is what was confirmed"


def test_a_restructure_that_adds_a_stage_continues_as_a_fork_and_the_original_ends_linked_to_it(
    runner: Runner, storage: SqliteStorage, calls: Calls, check: Check
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, PICK, RENDER])
    run = at(runner, run.id, "waiting_gate")
    tag = StageDef("tag", "connector", {"calls": "remotion_tag"})
    change = restructure(run, "render", [tag, RENDER])

    proposal = runner.propose(run.id, change)
    assert proposal.violations == [] and not proposal.in_place
    assert proposal.fork_from == "pick", "a Fork copies finished stages, so the gate opens anew"
    assert proposal.before == {"stages": [{"name": "render", "type": "connector", **RENDER.config}]}
    assert [s["name"] for s in proposal.after["stages"]] == ["tag", "render"]
    assert [s.name for s in check.seen[-1]] == ["write", "pick", "tag", "render"]
    assert runner.list_all("world_1") == [fetch(runner, run.id)], "a proposal writes nothing"

    applied = runner.apply(run.id, change, proposal.revision, "operator")
    assert applied.fork is not None and applied.kind == "restructure"
    original = at(runner, run.id, "aborted")
    assert original.forks == [applied.fork] and original.changes == [applied]
    fork = at(runner, applied.fork.id, "waiting_gate")
    assert fork.forked_from == RunLink(run.id, "world_1", "Short")
    assert fork.forked_by == applied, "the Fork says which change made it"
    assert [(s.name, s.status) for s in fork.stages] == [
        ("write", "done"), ("pick", "waiting"), ("tag", "pending"), ("render", "pending"),
    ]  # fmt: skip

    runner.decide(fork.id, choice="idea 1")
    at(runner, fork.id, "done")
    assert calls.ran == ["write", "tag", "render"], "write was copied, not run again"
    changed = events(storage, "run.changed")
    assert [(c["run_id"], c["fork"]["id"]) for c in changed] == [(run.id, fork.id)]


def test_a_stage_edit_to_a_failed_run_continues_as_a_fork_from_that_stage_and_the_fork_runs(
    runner: Runner, calls: Calls
) -> None:
    broken = StageDef("render", "connector", {"calls": "remotion_render", "fail": True})
    run = at(runner, runner.start("world_1", "wf_1", 1, "Short", [WRITE, broken]).id, "failed")
    change = edit(run, "render", RENDER.config)

    proposal = runner.propose(run.id, change)
    assert proposal.violations == [] and not proposal.in_place
    assert (proposal.fork_from, proposal.after) == ("render", RENDER.config)

    applied = runner.apply(run.id, change, proposal.revision, "operator")
    assert applied.fork is not None
    fork = at(runner, applied.fork.id, "done")
    assert calls.configs["render"] == RENDER.config
    assert calls.ran == ["write", "render", "render"], "the fork runs only the edited stage"
    assert fork.forked_by == applied
    assert fetch(runner, run.id).status == "failed", "a run that ended stays as it ended"


def test_a_restructure_that_breaks_a_rule_or_changes_a_started_stage_is_refused(
    runner: Runner,
) -> None:
    run = at(runner, runner.start("world_1", "wf_1", 1, "Short", [PICK, RENDER]).id, "waiting_gate")
    nobody = restructure(run, "render", [StageDef("render", "connector", {"calls": "nobody"})])

    proposal = runner.propose(run.id, nobody)
    assert proposal.violations == ["no connector named 'nobody'"]
    with pytest.raises(ValueError, match="no connector named 'nobody'"):
        runner.apply(run.id, nobody, proposal.revision, "operator")

    dropped = restructure(run, "pick", [RENDER])
    refused = "pick has started; a Restructure changes only the stages to come"
    assert runner.propose(run.id, dropped).violations == [refused]
    with pytest.raises(Illegal, match=refused):
        runner.apply(run.id, dropped, proposal.revision, "operator")
    same = restructure(run, "render", [RENDER])
    assert runner.propose(run.id, same).violations == ["nothing would change"]
    assert [r.id for r in runner.list_all("world_1")] == [run.id]
    assert fetch(runner, run.id).status == "waiting_gate" and fetch(runner, run.id).changes == []


def test_a_stage_edit_of_a_failed_publish_forks_from_its_gate_so_a_person_confirms_again(
    runner: Runner, calls: Calls
) -> None:
    def previews(_run: Run, stage: StageDef, _edits: dict[str, Any]) -> PublishPreview | None:
        title = str(stage.config["params"]["title"])
        return PublishPreview("youtube", [PublishField("title", "Title", title)])

    runner.previews = previews
    publish = StageDef("publish", "connector", {"calls": "youtube", "params": {"title": "Hooks"}})
    broken = StageDef("publish", "connector", {**publish.config, "fail": True})
    run = at(
        runner, runner.start("world_1", "wf_1", 1, "Short", [CONFIRM, broken]).id, "waiting_gate"
    )
    runner.decide(run.id)
    run = at(runner, run.id, "failed")

    proposal = runner.propose(run.id, edit(run, "publish", publish.config))
    assert proposal.violations == [] and proposal.fork_from == "confirm"
    applied = runner.apply(run.id, edit(run, "publish", publish.config), run.revision, "operator")
    assert applied.fork is not None
    fork = at(runner, applied.fork.id, "waiting_gate")
    assert [(s.name, s.status) for s in fork.stages] == [
        ("confirm", "waiting"),
        ("publish", "pending"),
    ]
    assert calls.ran == ["publish"], "nothing posts until a person confirms the new values"


def test_a_restructure_never_touches_the_running_stage_and_needs_a_stage(
    runner: Runner, calls: Calls
) -> None:
    slow = StageDef("slow", "connector", {"calls": "remotion_render"})
    run = runner.start("world_1", "wf_1", 1, "Short", [slow, RENDER])
    assert calls.started.wait(10)
    run = fetch(runner, run.id)
    tag = StageDef("tag", "connector", {"calls": "remotion_tag"})
    change = restructure(run, "render", [tag, RENDER])

    refused = "slow is running: pause first or add a task"
    assert runner.propose(run.id, change).violations == [refused]
    with pytest.raises(Illegal, match=refused):
        runner.apply(run.id, change, run.revision, "operator")
    with pytest.raises(ValueError, match="a Restructure needs at least one stage"):
        runner.propose(run.id, restructure(run, "render", []))

    calls.release.set()
    at(runner, run.id, "done")
    assert calls.ran == ["slow", "render"] and runner.list_all("world_1") == [fetch(runner, run.id)]
