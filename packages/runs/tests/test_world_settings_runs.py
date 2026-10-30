"""What the World settings do to runs, at the runner's seam with real DBOS on SQLite: the
Monthly Budget caps each new run's Budget, the run cap is one queue per world that updates live,
and an Ad Hoc Run pins no workflow."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from dbos import DBOS

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import MonthSpent, Run, Runner, SqliteStorage, StageDef, queue_name

RENDER = StageDef("render", "connector", {"calls": "remotion_render"})
PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})


class BlockingConnector:
    """Holds every call until `release` is set, so runs stay running as long as a test needs."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started: list[str] = []

    def __call__(self, run: Run, stage_id: str, stage: StageDef) -> None:
        self.started.append(run.id)
        assert self.release.wait(10)


@pytest.fixture
def settings() -> dict[str, object]:
    return {"monthly": {"world_1": 5.0}, "caps": {"world_1": 1}}


@pytest.fixture
def blocking() -> BlockingConnector:
    return BlockingConnector()


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, blocking: BlockingConnector, settings: dict
) -> Iterator[Runner]:
    runner = Runner(
        storage,
        tmp_path / "dbos.sqlite",
        blocking,
        tmp_path,
        FakeNotifier(),
        poll_seconds=0.05,
        monthly=lambda w: settings["monthly"].get(w),
        cap_for=lambda w: settings["caps"].get(w, 2),
    )
    runner.launch()
    yield runner
    blocking.release.set()
    runner.shutdown()


def test_an_ad_hoc_run_pins_no_workflow_and_runs_like_any_other(
    tmp_path: Path, storage: SqliteStorage
) -> None:
    connector = FakeConnector()
    runner = Runner(storage, tmp_path / "dbos.sqlite", connector, tmp_path, poll_seconds=0.05)
    runner.launch()
    try:
        run = runner.start("world_1", None, None, "Find hooks", [RENDER, PICK])
        assert (run.workflow_id, run.workflow_version) == (None, None)
        read = runner.get(run.id)
        assert read is not None and (read.workflow_id, read.workflow_version) == (None, None)
        assert runner.baseline(run.id) is None
        wait_for(lambda: (r := runner.get(run.id)) is not None and r.status == "waiting_gate")
        assert connector.calls == [(run.stages[0].id, "render")]
    finally:
        runner.shutdown()


def test_the_monthly_budget_caps_each_run_and_refuses_runs_once_spent(
    runner: Runner, storage: SqliteStorage
) -> None:
    capped = runner.start("world_1", "wf_1", 1, "Weekly", [RENDER], budget_usd=20.0)
    assert capped.budget_usd == 5.0
    storage.set_spent(capped.id, 4.0)
    rest = runner.start("world_1", "wf_1", 1, "Weekly", [RENDER])
    assert rest.budget_usd == 1.0
    storage.set_spent(rest.id, 1.0)
    with pytest.raises(MonthSpent, match="monthly budget of \\$5.00 is spent"):
        runner.start("world_1", "wf_1", 1, "Weekly", [RENDER], budget_usd=1.0)
    assert len(runner.list_all("world_1")) == 2
    assert storage.spent_in_month("world_1", capped.created_at[:7]) == 5.0
    assert storage.spent_in_month("world_1", "1999-01") == 0.0
    free = runner.start("world_2", "wf_2", 1, "Weekly", [RENDER], budget_usd=20.0)
    assert free.budget_usd == 20.0  # no monthly budget on this world


def test_the_run_cap_is_per_world_and_a_raised_cap_takes_effect_live(
    runner: Runner, blocking: BlockingConnector, settings: dict
) -> None:
    first = runner.start("world_1", "wf_1", 1, "Weekly", [RENDER])
    second = runner.start("world_1", "wf_1", 1, "Weekly", [RENDER])
    other = runner.start("world_2", "wf_2", 1, "Weekly", [RENDER])
    wait_for(lambda: set(blocking.started) == {first.id, other.id})
    waiting = runner.get(second.id)
    assert waiting is not None and waiting.status == "queued"

    settings["caps"]["world_1"] = 2
    runner.set_cap("world_1")
    wait_for(lambda: second.id in blocking.started)
    blocking.release.set()

    def ended(run_id: str) -> bool:
        run = runner.get(run_id)
        return run is not None and run.status == "done"

    for run in (first, second, other):
        wait_for(lambda run_id=run.id: ended(run_id))


def test_launch_drops_the_queues_of_deleted_worlds_and_keeps_the_live_ones(
    tmp_path: Path, storage: SqliteStorage
) -> None:
    """A world deleted before deletes took its queue left the queue in dbos.sqlite, each polled
    by its own thread for the life of the app. Launch drops those, and only those."""

    def runner(worlds: list[str] | None = None) -> Runner:
        return Runner(
            storage, tmp_path / "dbos.sqlite", connector, tmp_path, poll_seconds=0.05,
            cap_for=lambda _w: 1, worlds=None if worlds is None else lambda: worlds,
        )  # fmt: skip

    def status(run_id: str) -> str | None:
        return getattr(storage.get_run(run_id), "status", None)

    connector = FakeConnector()
    old = runner()
    old.launch()
    held = old.start("world_1", "wf_1", 1, "Held", [RENDER, PICK])
    wait_for(lambda: status(held.id) == "waiting_gate")
    queued = old.start("world_1", "wf_1", 1, "Queued", [RENDER])  # behind the cap of 1
    ran = [old.start(w, "wf", 1, "Done", [RENDER]) for w in ("world_2", "world_3")]
    for run in ran:
        wait_for(lambda run_id=run.id: status(run_id) == "done")
    old.shutdown()
    storage.delete_world("world_2")  # how a delete went before it took the queue

    new = runner(["world_1", "world_3"])
    new.launch()
    try:
        assert DBOS.retrieve_queue(queue_name("world_2")) is None
        assert DBOS.retrieve_queue(queue_name("world_3")) is not None  # live, nothing queued
        assert status(queued.id) == "queued"
        new.decide(held.id, "a")
        wait_for(lambda: status(queued.id) == "done")  # its queued work survived the sweep
    finally:
        new.shutdown()
