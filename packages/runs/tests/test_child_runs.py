"""Workflow stages at the runner's seam: real DBOS on SQLite, fake pin and take behind it."""

from pathlib import Path

import pytest

from runs_fakes import FakeConnector, wait_for
from swarmify_runs import Pinned, Run, Runner, SqliteStorage, StageDef

PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
RESEARCH = StageDef("research", "connector", {"calls": "web_search"})
REVIEW = StageDef("review", "gate", {"ask": "Approve?", "kind": "approve"})


def sub(name: str, runs: str, take: list[str] | None = None) -> StageDef:
    return StageDef(name, "workflow", {"runs": runs, "take": take or []})


class FakeTake:
    """Records every take; refuses a name that is not on the shelf."""

    def __init__(self) -> None:
        self.taken: list[tuple[str, str, str, list[str]]] = []

    def __call__(self, child: Run, parent: Run, stage_id: str, names: list[str]) -> None:
        if "missing.md" in names:
            raise ValueError("the child run produced no artifact named missing.md")
        self.taken.append((child.id, parent.id, stage_id, names))


@pytest.fixture
def take(runner: Runner) -> FakeTake:
    fake = FakeTake()
    runner.take = fake
    return fake


def library(runner: Runner, **workflows: Pinned) -> None:
    def pin(workflow_id: str) -> Pinned:
        return workflows[workflow_id]

    runner.pin = pin


def fetch(runner: Runner, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def at(runner: Runner, run_id: str, status: str) -> Run:
    wait_for(lambda: fetch(runner, run_id).status == status)
    return fetch(runner, run_id)


def child_of(runner: Runner, run_id: str, index: int) -> Run:
    wait_for(lambda: fetch(runner, run_id).stages[index].child is not None)
    link = fetch(runner, run_id).stages[index].child
    assert link is not None
    return fetch(runner, link.id)


def test_a_workflow_stage_runs_its_child_waits_and_takes_the_named_artifacts(
    runner: Runner, take: FakeTake, connector: FakeConnector
) -> None:
    library(runner, wf_trends=Pinned("world_b", "wf_trends", 4, "Trends", [RESEARCH, PICK]))
    parent = runner.start(
        "world_a", "wf_short", 1, "Short", [sub("trends", "wf_trends", ["brief.md"]), REVIEW]
    )

    child = child_of(runner, parent.id, 0)
    assert (child.world_id, child.workflow_id, child.workflow_version) == (
        "world_b",
        "wf_trends",
        4,
    )
    assert child.parent_run_id == parent.id and child.parent is not None
    assert (child.parent.id, child.parent.world_id, child.parent.name) == (
        parent.id,
        "world_a",
        "Short",
    )
    at(runner, child.id, "waiting_gate")
    running = fetch(runner, parent.id)
    assert running.status == "running" and running.stages[0].status == "running"
    link = running.stages[0].child
    assert link is not None and (link.world_id, link.name) == ("world_b", "Trends")
    (gate,) = runner.gates()
    assert gate.run_id == child.id and gate.parent is not None and gate.parent.name == "Short"
    assert take.taken == []

    runner.decide(child.id, choice="idea 1")
    at(runner, child.id, "done")
    waiting = at(runner, parent.id, "waiting_gate")
    assert take.taken == [(child.id, parent.id, waiting.stages[0].id, ["brief.md"])]
    assert waiting.stages[0].status == "done"
    runner.decide(parent.id)
    at(runner, parent.id, "done")


def test_depth_two_runs_a_grandchild_and_a_third_level_is_refused(
    runner: Runner, take: FakeTake
) -> None:
    library(
        runner,
        wf_mid=Pinned("world_a", "wf_mid", 1, "Mid", [sub("leaf", "wf_leaf", ["leaf.md"])]),
        wf_leaf=Pinned("world_a", "wf_leaf", 1, "Leaf", [PICK]),
        wf_loop=Pinned("world_a", "wf_loop", 1, "Loop", [sub("again", "wf_loop")]),
    )
    top = runner.start("world_a", "wf_top", 1, "Top", [sub("mid", "wf_mid", ["leaf.md"])])
    mid = child_of(runner, top.id, 0)
    leaf = child_of(runner, mid.id, 0)
    assert leaf.parent is not None and leaf.parent.id == mid.id
    at(runner, leaf.id, "waiting_gate")
    assert runner.family(top.id) == [top.id, mid.id, leaf.id]
    runner.decide(leaf.id, choice="x")
    for run_id in (leaf.id, mid.id, top.id):
        at(runner, run_id, "done")
    assert [(c, p) for c, p, _, _ in take.taken] == [(leaf.id, mid.id), (mid.id, top.id)]

    # a workflow edited after a run pinned it may nest deeper; the run stops at two
    looping = runner.start("world_a", "wf_loop", 1, "Loop", [sub("again", "wf_loop")])
    one = child_of(runner, looping.id, 0)
    two = child_of(runner, one.id, 0)
    failed = at(runner, two.id, "failed")
    assert "nest at most 2 deep" in (failed.stages[0].error or "")
    assert at(runner, one.id, "failed").stages[0].error == "child run Loop ended failed"
    at(runner, looping.id, "failed")


def test_a_failed_child_or_a_missing_artifact_fails_the_stage(
    runner: Runner, take: FakeTake
) -> None:
    library(
        runner,
        wf_broken=Pinned("world_a", "wf_broken", 1, "Broken", [StageDef("explode", "connector")]),
        wf_fine=Pinned("world_a", "wf_fine", 1, "Fine", [RESEARCH]),
    )
    broken = runner.start("world_a", "wf_p", 1, "P1", [sub("b", "wf_broken")])
    failed = at(runner, broken.id, "failed")
    assert failed.stages[0].error == "child run Broken ended failed"

    missing = runner.start("world_a", "wf_p", 1, "P2", [sub("f", "wf_fine", ["missing.md"])])
    failed = at(runner, missing.id, "failed")
    assert "no artifact named missing.md" in (failed.stages[0].error or "")

    unknown = runner.start("world_a", "wf_p", 1, "P3", [sub("u", "wf_nope")])
    assert "KeyError" in (at(runner, unknown.id, "failed").stages[0].error or "")


def test_abort_cascades_to_the_child_and_the_parent_ends_aborted(
    runner: Runner, take: FakeTake
) -> None:
    library(runner, wf_c=Pinned("world_b", "wf_c", 1, "Child", [PICK]))
    parent = runner.start("world_a", "wf_p", 1, "Parent", [sub("c", "wf_c"), REVIEW])
    child = child_of(runner, parent.id, 0)
    at(runner, child.id, "waiting_gate")
    runner.abort(parent.id)
    at(runner, child.id, "aborted")
    aborted = at(runner, parent.id, "aborted")
    assert [s.status for s in aborted.stages] == ["failed", "pending"]
    assert runner.gates() == []


def test_aborting_only_the_child_fails_the_parents_stage(runner: Runner, take: FakeTake) -> None:
    library(runner, wf_c=Pinned("world_a", "wf_c", 1, "Child", [PICK]))
    parent = runner.start("world_a", "wf_p", 1, "Parent", [sub("c", "wf_c")])
    child = child_of(runner, parent.id, 0)
    at(runner, child.id, "waiting_gate")
    runner.abort(child.id)
    failed = at(runner, parent.id, "failed")
    assert failed.stages[0].error == "child run Child ended aborted"


def test_a_sent_back_workflow_stage_starts_a_fresh_child(runner: Runner, take: FakeTake) -> None:
    library(runner, wf_c=Pinned("world_a", "wf_c", 1, "Child", [RESEARCH]))
    parent = runner.start("world_a", "wf_p", 1, "Parent", [sub("c", "wf_c"), REVIEW])
    first = at(runner, parent.id, "waiting_gate").stages[0].child
    assert first is not None
    runner.decide(parent.id, send_back_to="c")
    wait_for(lambda: fetch(runner, parent.id).send_backs == 1)
    second = at(runner, parent.id, "waiting_gate").stages[0].child
    assert second is not None and second.id != first.id
    assert sorted(runner.family(parent.id)[1:]) == sorted([first.id, second.id])


def test_children_never_wait_behind_their_parents_for_a_world_slot(
    runner: Runner, take: FakeTake
) -> None:
    """Both of world_a's slots hold parents; their children in world_a still run."""
    library(runner, wf_c=Pinned("world_a", "wf_c", 1, "Child", [RESEARCH]))
    parents = [runner.start("world_a", "wf_p", 1, f"P{i}", [sub("c", "wf_c")]) for i in range(2)]
    for p in parents:
        at(runner, p.id, "done")


def test_without_a_pin_a_workflow_stage_fails_honestly(runner: Runner, tmp_path: Path) -> None:
    run = runner.start("world_a", "wf_p", 1, "P", [sub("c", "wf_c")])
    assert "need a pin" in (at(runner, run.id, "failed").stages[0].error or "")


def test_a_parent_waiting_on_its_child_survives_a_restart_without_a_second_child(
    tmp_path: Path, storage: SqliteStorage, connector: FakeConnector
) -> None:
    pinned = Pinned("world_b", "wf_c", 1, "Child", [PICK])

    def runner() -> Runner:
        r = Runner(storage, tmp_path / "dbos.sqlite", connector, poll_seconds=0.05)
        r.pin, r.take = (lambda _id: pinned), FakeTake()
        r.launch()
        return r

    first = runner()
    parent = first.start("world_a", "wf_p", 1, "Parent", [sub("c", "wf_c", ["brief.md"])])
    child = child_of(first, parent.id, 0)
    at(first, child.id, "waiting_gate")
    first.shutdown()

    second = runner()
    try:
        second.decide(child.id, choice="x")
        at(second, parent.id, "done")
        assert storage.child_run_ids(parent.id) == [child.id]
    finally:
        second.shutdown()
