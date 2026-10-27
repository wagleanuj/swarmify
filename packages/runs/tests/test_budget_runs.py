"""The Budget at the runner's seam: real DBOS on SQLite, a fake swarm that spends a dollar per
tool call and asks the Budget after each, as the worker's cost hook does, and a fake notifier."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import Illegal, OverBudget, Run, Runner, SqliteStorage, StageDef
from swarmify_runs.control import RunControl

RESEARCH = StageDef("research", "swarm", {"goal": "Find ideas."})
PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})


class FakeSwarm:
    """Each stage needs `calls` tool calls at $1 each and remembers how far it got, so a stage
    stopped by the Budget carries on where it stopped, like a resumed session."""

    def __init__(self, calls: int) -> None:
        self.calls = calls
        self.cost: dict[str, float] = {}
        self.done: dict[str, int] = {}
        self.visits = 0
        self.runner: RunControl | None = None

    def spend(self, run_ids: list[str]) -> float:
        return sum(self.cost.get(r, 0.0) for r in run_ids)

    def __call__(self, run: Run, stage_id: str, stage: StageDef) -> None:
        assert self.runner is not None
        self.visits += 1
        if self.runner.check_budget(run.id):  # checked before a session starts
            raise OverBudget()
        while self.done.get(stage_id, 0) < self.calls:
            self.done[stage_id] = self.done.get(stage_id, 0) + 1
            self.cost[run.id] = self.cost.get(run.id, 0.0) + 1.0
            if self.runner.check_budget(run.id):
                raise OverBudget()


@pytest.fixture
def swarm() -> FakeSwarm:
    return FakeSwarm(calls=4)


@pytest.fixture
def budgeted(
    tmp_path: Path,
    storage: SqliteStorage,
    connector: FakeConnector,
    notifier: FakeNotifier,
    swarm: FakeSwarm,
) -> Iterator[Runner]:
    runner = Runner(
        storage, tmp_path / "dbos.sqlite", connector, tmp_path, notifier,
        swarm=swarm, poll_seconds=0.05, spend=swarm.spend,
    )  # fmt: skip
    swarm.runner = runner
    runner.launch()
    yield runner
    runner.shutdown()


def fetch(runner: RunControl, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def types(notifier: FakeNotifier) -> list[str]:
    return [e.type for e in notifier.events]


def test_eighty_percent_warns_once_and_the_run_goes_on(
    budgeted: Runner, notifier: FakeNotifier
) -> None:
    run = budgeted.start("world_1", "wf_1", 1, "Research", [RESEARCH], budget_usd=4.5)
    wait_for(lambda: types(notifier) == ["budget.warning", "run.ended"])
    done = fetch(budgeted, run.id)
    assert done.spent_usd == 4.0 and done.budget_usd == 4.5 and not done.over_budget
    warning = notifier.events[0]
    assert warning.run_id == run.id
    assert warning.payload == {
        "run_id": run.id, "name": "Research", "spent_usd": 4.0, "budget_usd": 4.5,
    }  # fmt: skip


def test_a_run_without_a_budget_never_stops(budgeted: Runner, notifier: FakeNotifier) -> None:
    run = budgeted.start("world_1", "wf_1", 1, "Research", [RESEARCH])
    wait_for(lambda: types(notifier) == ["run.ended"])
    assert fetch(budgeted, run.id).spent_usd == 4.0


def test_a_hundred_percent_stops_the_agents_at_a_budget_gate_until_money_is_added(
    budgeted: Runner, notifier: FakeNotifier, swarm: FakeSwarm
) -> None:
    run = budgeted.start("world_1", "wf_1", 1, "Research", [RESEARCH, PICK], budget_usd=3)
    wait_for(lambda: types(notifier) == ["budget.warning", "gate.opened"])  # notified after save
    stopped = fetch(budgeted, run.id)
    assert stopped.over_budget and stopped.spent_usd == 3.0
    assert [s.status for s in stopped.stages] == ["waiting", "pending"]
    assert swarm.done == {stopped.stages[0].id: 3}
    opened = notifier.events[1].payload
    assert opened["kind"] == "budget" and "$3.00 is spent ($3.00 so far" in opened["ask"]

    (gate,) = budgeted.gates()
    assert (gate.kind, gate.stage_name, gate.budget_usd, gate.spent_usd) == (
        "budget", "research", 3.0, 3.0,
    )  # fmt: skip
    assert gate.ask.endswith("Add money to go on, or abort.") and gate.earlier_stages == []
    with pytest.raises(Illegal, match="budget gate"):
        budgeted.decide(run.id, choice="idea 1")
    with pytest.raises(ValueError, match=r"spent \$3\.00"):
        budgeted.set_budget(run.id, 3)

    budgeted.set_budget(run.id, 10)
    wait_for(lambda: fetch(budgeted, run.id).status == "waiting_gate" and gate_is_pick(budgeted))
    resumed = fetch(budgeted, run.id)
    assert not resumed.over_budget and resumed.budget_usd == 10 and resumed.spent_usd == 4.0
    assert swarm.visits == 2 and swarm.done == {stopped.stages[0].id: 4}
    assert budgeted.set_budget(run.id, 10).budget_usd == 10  # the same total again adds nothing
    budgeted.decide(run.id, choice="idea 1")
    wait_for(lambda: fetch(budgeted, run.id).status == "done")


def gate_is_pick(runner: RunControl) -> bool:
    return [g.kind for g in runner.gates()] == ["pick"]


def test_a_stage_starting_over_budget_stops_before_its_agents_spend(
    budgeted: Runner, swarm: FakeSwarm
) -> None:
    second = StageDef("more", "swarm", {"goal": "More ideas."})
    run = budgeted.start("world_1", "wf_1", 1, "Research", [RESEARCH, second], budget_usd=4)
    # the first stage's last call reaches the budget exactly; the stage stops there
    wait_for(lambda: fetch(budgeted, run.id).over_budget)
    assert fetch(budgeted, run.id).stages[0].status == "waiting"
    budgeted.set_budget(run.id, 4.5)  # enough to finish nothing more than the first stage
    wait_for(lambda: fetch(budgeted, run.id).stages[1].status == "waiting")
    stopped = fetch(budgeted, run.id)
    assert stopped.over_budget and stopped.stages[0].status == "done"
    assert swarm.done[stopped.stages[1].id] == 1  # one call, then the Budget stopped it


def test_aborting_at_a_budget_gate_ends_the_run(budgeted: Runner) -> None:
    run = budgeted.start("world_1", "wf_1", 1, "Research", [RESEARCH], budget_usd=1)
    wait_for(lambda: fetch(budgeted, run.id).over_budget)
    budgeted.abort(run.id)
    wait_for(lambda: fetch(budgeted, run.id).status == "aborted")
    assert not fetch(budgeted, run.id).over_budget and budgeted.gates() == []


def test_a_child_run_spends_its_parents_budget(
    storage: SqliteStorage, tmp_path: Path, notifier: FakeNotifier
) -> None:
    now = datetime.now(UTC).isoformat()
    parent = storage.add_run(
        Run("run_p", "world_1", "wf_1", 1, "Parent", "running", now, now, [], budget_usd=2), []
    )
    child = storage.add_run(
        Run("run_c", "world_2", "wf_2", 1, "Child", "running", now, now, [], parent_run_id="run_p"),
        [],
    )
    cost = {"run_p": 0.5, "run_c": 0.0}
    control = RunControl(
        storage, tmp_path / "dbos.sqlite", tmp_path, notifier,
        spend=lambda ids: sum(cost.get(i, 0.0) for i in ids),
    )  # fmt: skip
    assert control.check_budget(child.id) is None
    cost["run_c"] = 1.2
    assert control.check_budget(child.id) is None
    assert types(notifier) == ["budget.warning"] and notifier.events[0].run_id == parent.id
    cost["run_c"] = 1.5
    assert control.check_budget(child.id) == "The run's budget of $2.00 is spent."
    assert fetch(control, parent.id).spent_usd == 2.0
    assert fetch(control, child.id).spent_usd == 1.5
    assert types(notifier) == ["budget.warning"]  # once per budget
