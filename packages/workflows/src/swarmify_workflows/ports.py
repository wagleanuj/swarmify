"""Storage port of the Workflows context (ADR 0012). SQLite is the adapter; Postgres is the seam."""

from typing import Protocol

from swarmify_kernel import Event
from swarmify_workflows.model import Workflow, WorkflowVersion


class Storage(Protocol):
    def add_workflow(self, workflow: Workflow, yaml: str, event: Event) -> WorkflowVersion:
        """Insert the workflow, its version 1 and `event` in one transaction."""
        ...

    def add_version(
        self, workflow_id: str, yaml: str, name: str, event: Event
    ) -> WorkflowVersion | None:
        """Append the next version, point the workflow at it and rename it, in one transaction.

        None when the workflow does not exist.
        """
        ...

    def list_workflows(self, world_id: str) -> list[Workflow]: ...

    def get_workflow(self, workflow_id: str) -> Workflow | None: ...

    def list_versions(self, workflow_id: str) -> list[WorkflowVersion]: ...

    def get_version(self, workflow_id: str, number: int) -> WorkflowVersion | None: ...

    def current_yaml(self) -> dict[str, str]:
        """Every workflow id, across worlds, to the YAML of its current version."""
        ...
