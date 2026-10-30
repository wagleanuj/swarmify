"""Storage port of the Workflows context (ADR 0012). SQLite is the adapter; Postgres is the seam."""

from collections.abc import Mapping
from typing import Protocol

from swarmify_kernel import Event
from swarmify_workflows.model import Workflow, WorkflowVersion


class Storage(Protocol):
    def add_workflow(
        self, workflow: Workflow, yaml: str, event: Event, packages: Mapping[str, str] | None = None
    ) -> WorkflowVersion:
        """Insert the workflow, its version 1 recording `packages` and `event` in one
        transaction."""
        ...

    def add_version(
        self,
        workflow_id: str,
        yaml: str,
        name: str,
        event: Event,
        packages: Mapping[str, str] | None = None,
    ) -> WorkflowVersion | None:
        """Append the next version, point the workflow at it and rename it, in one transaction.

        None when the workflow does not exist.
        """
        ...

    def list_workflows(self, world_id: str) -> list[Workflow]: ...

    def get_workflow(self, workflow_id: str) -> Workflow | None: ...

    def list_versions(self, workflow_id: str) -> list[WorkflowVersion]: ...

    def get_version(self, workflow_id: str, number: int) -> WorkflowVersion | None: ...

    def delete_world(self, world_id: str) -> None:
        """Remove every workflow of the world and, with them, their versions."""
        ...

    def current_yaml(self) -> dict[str, str]:
        """Every workflow id, across worlds, to the YAML of its current version."""
        ...

    def all_versions(self) -> list[tuple[str, WorkflowVersion]]:
        """Every version of every workflow, across worlds, with its workflow's world id."""
        ...

    def set_packages(self, version_id: str, packages: Mapping[str, str]) -> None:
        """Replace the package versions a version records; only the stamp calls it."""
        ...
