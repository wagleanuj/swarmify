"""Ports of the Runs context (ADR 0012): Storage (SQLite now, Postgres is the seam) and
Notifier (Slack in the Notifications context, a log line standalone, a recording fake in tests)."""

from typing import Protocol

from swarmify_kernel import Event
from swarmify_runs.machine import RunState
from swarmify_runs.model import Decision, Gate, Run, StageDef


class Storage(Protocol):
    def add_run(self, run: Run, stages: list[StageDef], skipped: int = 0) -> Run:
        """Insert a queued run, its stages and a `run.updated` event in one transaction. The
        first `skipped` stages are inserted skipped (a promote), the rest pending."""
        ...

    def fork(self, run: Run, source: Run, index: int) -> Run:
        """Insert `run` as a fork of `source`: the stages before `index` copied with their
        status, times and decisions, the rest pending; the lineage recorded on the run."""
        ...

    def save_state(self, run_id: str, state: RunState, error: str | None = None) -> Run | None:
        """Write the run's new state and its `run.updated` event in one transaction.

        Stamps started_at and ended_at on stages whose status changed; `error` lands on a stage
        that just failed. None when the run no longer exists.
        """
        ...

    def add_decision(self, run_id: str, decision: Decision, visit: int) -> None:
        """`visit` is the run's send-back count, which tells visits to one gate apart. Raises
        Illegal when this visit already has a decision."""
        ...

    def set_spent(self, run_id: str, usd: float) -> Run | None:
        """Write the run's estimated spend; a change appends `run.updated`."""
        ...

    def set_budget(self, run_id: str, usd: float) -> Run | None:
        """Write the run's Budget; a change appends `run.updated`."""
        ...

    def mark_warned(self, run_id: str, budget_usd: float) -> bool:
        """Record that the 80 percent warning went out for this budget. False when it already
        had, so of any number of concurrent callers exactly one warns."""
        ...

    def set_stop(self, run_id: str, stop: str | None) -> None:
        """Ask the run's agents to stop mid-stage (`pause` or `abort`), or let them go (None)."""
        ...

    def stop_of(self, run_id: str) -> str | None:
        """The stop asked for by `set_stop`, None when none is."""
        ...

    def latest_baseline(self, workflow_id: str | None) -> Run | None:
        """The newest finished baseline run of the workflow, any version. None for an ad hoc
        run, which has no workflow to be measured against."""
        ...

    def spent_in_month(self, world_id: str, month: str) -> float:
        """The estimated spend of the world's top runs created in `month` (`YYYY-MM`). Child
        runs are counted through their parent's spend (ADR 0013)."""
        ...

    def set_child(self, run_id: str, index: int, child_run_id: str) -> None:
        """Link the workflow stage at `index` to the child run it started."""
        ...

    def remove_decision(self, decision_id: str) -> None:
        """Undo `add_decision` when the decision could not reach the run."""
        ...

    def record(self, event: Event) -> None:
        """Append one event to the world's stream."""
        ...

    def get_run(self, run_id: str) -> Run | None: ...

    def stage_def(self, run_id: str, index: int) -> tuple[str, StageDef]:
        """The stage id and its definition."""
        ...

    def list_runs(self, world_id: str) -> list[Run]:
        """Newest first."""
        ...

    def open_gates(self) -> list[Gate]:
        """Every gate waiting on a person, across all worlds, newest first."""
        ...

    def child_run_ids(self, run_id: str) -> list[str]: ...

    def queued_run_ids(self) -> list[str]: ...

    def active_run_ids(self, world_id: str) -> list[str]:
        """Runs of the world that have not ended."""
        ...

    def delete_world(self, world_id: str) -> None: ...


class Notifier(Protocol):
    def notify(self, event: Event) -> None:
        """Tell a person: `gate.opened` (kind `budget` at 100 percent of the Budget),
        `budget.warning` (80 percent) or `run.ended`. Payloads carry run id and name.
        May raise (Slack is down); callers log it and go on."""
        ...
