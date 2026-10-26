"""Workflows context: a Workflow is a saved, versioned recipe of Stages.

Saving YAML parses it into the four stage types, runs every rule, and on success appends an
immutable Workflow Version. Connectors belong to another context, so the composition root hands
in a function from world id to the connectors that world allows.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from swarmify_kernel import Event, new_id
from swarmify_workflows.model import (
    ConnectorRef,
    ConnectorStage,
    GateStage,
    Stage,
    SwarmStage,
    Violation,
    Workflow,
    WorkflowModel,
    WorkflowStage,
    WorkflowVersion,
)
from swarmify_workflows.parse import parse_workflow
from swarmify_workflows.ports import Storage
from swarmify_workflows.sqlite import MIGRATIONS, SqliteStorage
from swarmify_workflows.validate import validate

__all__ = [
    "MIGRATIONS",
    "ConnectorRef",
    "ConnectorStage",
    "Draft",
    "GateStage",
    "Invalid",
    "SqliteStorage",
    "Stage",
    "Storage",
    "SwarmStage",
    "Violation",
    "Workflow",
    "WorkflowModel",
    "WorkflowStage",
    "WorkflowVersion",
    "Workflows",
]


@dataclass(frozen=True)
class Draft:
    """A checked but unsaved workflow: its model when the shape parsed, and every violation."""

    model: WorkflowModel | None
    violations: list[Violation]


class Invalid(ValueError):
    def __init__(self, violations: list[Violation]) -> None:
        super().__init__("; ".join(v.message for v in violations))
        self.violations = violations


class Workflows:
    def __init__(
        self, storage: Storage, connectors_for: Callable[[str], Mapping[str, ConnectorRef]]
    ) -> None:
        self.storage = storage
        self.connectors_for = connectors_for

    def check(self, world_id: str, yaml: str, workflow_id: str | None = None) -> Draft:
        """Parse and validate without saving. `workflow_id` is the workflow a new version is for."""
        model, violations = parse_workflow(yaml)
        if model is not None:
            violations += validate(
                model,
                workflow_id=workflow_id,
                connectors=self.connectors_for(world_id),
                references=self._references(),
            )
        return Draft(model, violations)

    def save(self, world_id: str, yaml: str, workflow_id: str | None = None) -> WorkflowVersion:
        """Create a workflow (no id) or append a version to one. Raises Invalid; saves nothing."""
        draft = self.check(world_id, yaml, workflow_id)
        if draft.violations or draft.model is None:
            raise Invalid(draft.violations)
        if workflow_id is None:
            workflow = Workflow(
                new_id("wf"), world_id, draft.model.name, 1, datetime.now(UTC).isoformat()
            )
            return self.storage.add_workflow(
                workflow, yaml, _saved(workflow.id, world_id, draft.model.name, 1)
            )
        existing = self.storage.get_workflow(workflow_id)
        if existing is None or existing.world_id != world_id:
            raise KeyError(workflow_id)
        number = existing.current_version + 1
        event = _saved(workflow_id, world_id, draft.model.name, number)
        version = self.storage.add_version(workflow_id, yaml, draft.model.name, event)
        if version is None:
            raise KeyError(workflow_id)
        return version

    def list_all(self, world_id: str) -> list[Workflow]:
        return self.storage.list_workflows(world_id)

    def get(self, workflow_id: str) -> Workflow | None:
        return self.storage.get_workflow(workflow_id)

    def versions(self, workflow_id: str) -> list[WorkflowVersion]:
        return self.storage.list_versions(workflow_id)

    def version(self, workflow_id: str, number: int) -> WorkflowVersion | None:
        return self.storage.get_version(workflow_id, number)

    def _references(self) -> dict[str, list[str]]:
        # ponytail: re-parses every current version on each save. Fine for one operator's
        # dozens of workflows; cache per workflow id if saves ever feel slow.
        refs: dict[str, list[str]] = {}
        for workflow_id, yaml in self.storage.current_yaml().items():
            model, _ = parse_workflow(yaml)
            stages = model.stages if model else []
            refs[workflow_id] = [s.runs for s in stages if isinstance(s, WorkflowStage)]
        return refs


def _saved(workflow_id: str, world_id: str, name: str, number: int) -> Event:
    return Event.new(
        "workflow.saved",
        world_id=world_id,
        payload={"workflow_id": workflow_id, "name": name, "version": number},
    )
