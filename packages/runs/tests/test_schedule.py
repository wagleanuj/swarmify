"""Workflow schedules on real DBOS cron (ADR 0014): a fired schedule starts an ordinary run."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import Pinned, Runner, SqliteStorage, StageDef

PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
EVERY_SECOND = "* * * * * *"


def pin(workflow_id: str, number: int | None = None) -> Pinned:
    if workflow_id not in ("wf_1", "wf_dropped"):
        raise KeyError(workflow_id)
    # wf_dropped's current version no longer has a schedule, but DBOS still holds the old cron.
    cron = None if workflow_id == "wf_dropped" else EVERY_SECOND
    return Pinned("world_1", workflow_id, 2, "Weekly short", [PICK], budget_usd=5.0, schedule=cron)


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, connector: FakeConnector, notifier: FakeNotifier
) -> Iterator[Runner]:
    runner = Runner(
        storage, tmp_path / "dbos.sqlite", connector, tmp_path, notifier, poll_seconds=0.05, pin=pin
    )
    runner.launch()
    yield runner
    runner.shutdown()


def test_a_schedule_starts_runs_of_the_current_version_until_removed(runner: Runner) -> None:
    assert runner.schedules() == {}
    runner.schedule("wf_1", EVERY_SECOND)
    assert runner.schedules() == {"wf_1": EVERY_SECOND}

    wait_for(lambda: len(runner.list_all("world_1")) >= 1)
    run = runner.list_all("world_1")[-1]
    assert (run.workflow_id, run.workflow_version, run.name) == ("wf_1", 2, "Weekly short")
    assert run.budget_usd == 5.0
    wait_for(lambda: runner.get(run.id) is not None and runner.get(run.id).status == "waiting_gate")  # type: ignore[union-attr]

    runner.schedule("wf_1", None)
    assert runner.schedules() == {}
    time.sleep(1.5)
    count = len(runner.list_all("world_1"))
    time.sleep(2.5)
    assert len(runner.list_all("world_1")) == count


def test_a_fired_schedule_for_a_deleted_workflow_starts_nothing(runner: Runner) -> None:
    runner.schedule("wf_gone", EVERY_SECOND)
    time.sleep(2.5)
    assert runner.list_all("world_1") == []


def test_a_tick_after_the_version_dropped_its_schedule_starts_nothing(runner: Runner) -> None:
    runner.schedule("wf_dropped", EVERY_SECOND)
    time.sleep(2.5)
    assert runner.list_all("world_1") == []


def test_sync_makes_dbos_schedules_match_the_workflows(runner: Runner) -> None:
    runner.schedule("wf_old", "0 8 * * 1")
    runner.schedule("wf_1", "0 8 * * 1")
    runner.sync_schedules({"wf_1": "0 9 * * 1", "wf_new": "0 10 * * 2"})
    assert runner.schedules() == {"wf_1": "0 9 * * 1", "wf_new": "0 10 * * 2"}
    with pytest.raises(ValueError):
        runner.schedule("wf_1", "every monday")
    assert "wf_1" not in runner.schedules()


def test_a_busy_database_fails_the_schedule_instead_of_deferring_it(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(_name: str) -> None:
        raise OperationalError("DELETE", {}, Exception("database is locked"))

    monkeypatch.setattr(runner.client, "delete_schedule", locked)
    with pytest.raises(OperationalError, match="database is locked"):
        runner.schedule("wf_1", EVERY_SECOND)
