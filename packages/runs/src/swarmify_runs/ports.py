"""Storage port of the Runs context (ADR 0012). SQLite is the adapter; Postgres is the seam."""

from typing import Protocol

from swarmify_runs.machine import RunState
from swarmify_runs.model import Run, StageDef


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

    def get_run(self, run_id: str) -> Run | None: ...

    def stage_def(self, run_id: str, index: int) -> tuple[str, StageDef]:
        """The stage id and its definition."""
        ...

    def list_runs(self, world_id: str) -> list[Run]:
        """Newest first."""
        ...

    def queued_run_ids(self) -> list[str]: ...

    def active_run_ids(self, world_id: str) -> list[str]:
        """Runs of the world that have not ended."""
        ...

    def delete_world(self, world_id: str) -> None: ...
