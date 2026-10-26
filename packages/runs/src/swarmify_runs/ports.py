"""Ports of the Runs context (ADR 0012): Storage (SQLite now, Postgres is the seam) and
Notifier (a log line now, Slack in T16, a recording fake in tests)."""

from typing import Protocol

from swarmify_kernel import Event
from swarmify_runs.machine import RunState
from swarmify_runs.model import Decision, Gate, Run, StageDef


class Storage(Protocol):
    def add_run(self, run: Run, stages: list[StageDef]) -> Run:
        """Insert a queued run, its stages and a `run.updated` event in one transaction."""
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
        """Tell a person: `gate.opened` or `run.ended`. Payloads carry run id, name and stage."""
        ...
