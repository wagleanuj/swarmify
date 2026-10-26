"""Storage port of the Swarm context (ADR 0012). SQLite is the adapter; Postgres is the seam.

Every write records a `task.updated` event carrying the whole task in the same transaction.
Unknown task ids raise KeyError; refused actions raise Refused.
"""

from datetime import datetime
from typing import Protocol

from swarmify_swarm.model import Task


class Storage(Protocol):
    def add_task(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        title: str,
        *,
        added_by: str,
        body: str = "",
        depends_on: tuple[str, ...] = (),
    ) -> Task:
        """`depends_on` must name tasks of the same stage."""
        ...

    def get_task(self, task_id: str) -> Task | None: ...

    def list_tasks(self, stage_id: str) -> list[Task]:
        """Oldest first."""
        ...

    def claim(self, task_id: str, agent: str, now: datetime | None = None) -> Task:
        """One immediate transaction: exactly one of any number of concurrent claims wins.

        Refused when the task is not open or a task it depends on is not done.
        """
        ...

    def heartbeat(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task:
        """Refresh the claim of `agent`, attaching `note` (partial progress) when given."""
        ...

    def finish(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task: ...

    def expire(self, now: datetime | None = None) -> list[Task]:
        """Return to open every claimed task whose heartbeat is older than HEARTBEAT_TIMEOUT,
        keeping its notes. Returns the tasks it reopened."""
        ...

    def delete_world(self, world_id: str) -> None: ...
