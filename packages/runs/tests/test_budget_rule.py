"""One budget rule for every way a run starts (start, fork, promote, schedule, child): the
explicit budget, else the workflow's, else the default, capped by what is left of the world's
Monthly Budget, refused once the month is spent. Real DBOS on SQLite."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import MonthSpent, Pinned, Run, Runner, SqliteStorage, StageDef
from swarmify_runs.budget import DEFAULT_USD

PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
RENDER = StageDef("render", "connector", {"calls": "remotion_render"})
EVERY_SECOND = "* * * * * *"
CAPPED = "world_capped"
"""The world with a Monthly Budget of 3 USD; every other world has none."""


def pin(workflow_id: str, number: int | None = None) -> Pinned:
    world = CAPPED if workflow_id == "wf_capped" else "world_free"
    if workflow_id == "wf_child":
        return Pinned(world, workflow_id, 1, "Child", [PICK], budget_usd=50.0)
    return Pinned(world, workflow_id, 1, "Weekly", [PICK], budget_usd=8.0, schedule=EVERY_SECOND)


@pytest.fixture
def runner(tmp_path: Path, storage: SqliteStorage, connector: FakeConnector) -> Iterator[Runner]:
    runner = Runner(
        storage,
        tmp_path / "dbos.sqlite",
        connector,
        tmp_path,
        FakeNotifier(),
        poll_seconds=0.05,
        pin=pin,
        monthly=lambda w: 3.0 if w == CAPPED else None,
    )
    runner.launch()
    yield runner
    runner.shutdown()


def spend_the_month(runner: Runner, storage: SqliteStorage) -> None:
    storage.set_spent(runner.start(CAPPED, None, None, "Spender", [PICK]).id, 3.0)


def test_start_takes_the_explicit_budget_else_the_workflows_else_the_default(
    runner: Runner, storage: SqliteStorage
) -> None:
    assert DEFAULT_USD == 5.0
    free = "world_free"
    assert (
        runner.start(free, "wf", 1, "W", [PICK], budget_usd=2.0, workflow_budget_usd=8.0).budget_usd
        == 2.0
    )
    assert runner.start(free, "wf", 1, "W", [PICK], workflow_budget_usd=8.0).budget_usd == 8.0
    assert runner.start(free, None, None, "Ad hoc", [PICK]).budget_usd == DEFAULT_USD
    assert runner.start(CAPPED, None, None, "Ad hoc", [PICK]).budget_usd == 3.0
    spend_the_month(runner, storage)
    with pytest.raises(MonthSpent):
        runner.start(CAPPED, None, None, "Ad hoc", [PICK])


def test_a_fork_keeps_the_runs_budget_under_the_monthly_cap(
    runner: Runner, storage: SqliteStorage
) -> None:
    source = runner.start("world_free", "wf", 1, "W", [PICK, RENDER], workflow_budget_usd=8.0)
    assert runner.fork(source.id, 0).budget_usd == 8.0
    adhoc = runner.start(CAPPED, None, None, "Ad hoc", [PICK, RENDER])
    assert runner.fork(adhoc.id, 0).budget_usd == 3.0
    spend_the_month(runner, storage)
    with pytest.raises(MonthSpent):
        runner.fork(adhoc.id, 0)


def test_a_promote_takes_the_workflows_budget_under_the_monthly_cap(
    runner: Runner, storage: SqliteStorage, tmp_path: Path
) -> None:
    (tmp_path / "script.md").write_text("A script.")
    free = Pinned("world_free", "wf", 1, "W", [PICK, RENDER], budget_usd=8.0)
    assert runner.promote(free, 1, tmp_path / "script.md", "script.md").budget_usd == 8.0
    bare = Pinned("world_free", "wf", 1, "W", [PICK, RENDER])
    assert runner.promote(bare, 1, tmp_path / "script.md", "script.md").budget_usd == DEFAULT_USD
    capped = Pinned(CAPPED, "wf", 1, "W", [PICK, RENDER], budget_usd=8.0)
    assert runner.promote(capped, 1, tmp_path / "script.md", "script.md").budget_usd == 3.0
    spend_the_month(runner, storage)
    with pytest.raises(MonthSpent):
        runner.promote(capped, 1, tmp_path / "script.md", "script.md")
    assert [r.name for r in runner.list_all(CAPPED)].count("W") == 1


def test_a_schedule_is_capped_by_the_month_and_starts_nothing_once_it_is_spent(
    runner: Runner, storage: SqliteStorage
) -> None:
    runner.schedule("wf_capped", EVERY_SECOND)
    wait_for(lambda: any(r.name == "Weekly" for r in runner.list_all(CAPPED)))
    first = next(r for r in runner.list_all(CAPPED) if r.name == "Weekly")
    assert first.budget_usd == 3.0  # the workflow's 8, cut down to the month's 3
    storage.set_spent(first.id, 3.0)
    time.sleep(1.5)  # let a tick already past the rule land
    count = len(runner.list_all(CAPPED))
    time.sleep(2.5)
    assert len(runner.list_all(CAPPED)) == count
    runner.schedule("wf_capped", None)


def test_a_child_run_spends_its_parents_budget(runner: Runner) -> None:
    sub = StageDef("trends", "workflow", {"runs": "wf_child", "take": []})
    parent = runner.start("world_free", "wf", 1, "Short", [sub])
    assert parent.budget_usd == DEFAULT_USD

    def child() -> Run | None:
        link = (r := runner.get(parent.id)) and r.stages[0].child
        return runner.get(link.id) if link else None

    wait_for(lambda: child() is not None)
    found = child()
    assert found is not None and found.budget_usd is None  # the family's Budget is the parent's
    assert runner.top(found).id == parent.id
    runner.abort(parent.id)
