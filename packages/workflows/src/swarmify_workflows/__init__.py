"""Workflows context: a Workflow is a saved, versioned recipe of Stages.

Saving YAML parses it into the four stage types, runs every rule, and on success appends an
immutable Workflow Version. Connectors belong to another context, so the composition root hands
in a function from world id to the connectors that world allows.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from swarmify_kernel import Event, new_id
from swarmify_workflows.dump import dump_workflow
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
    calls,
)
from swarmify_workflows.parse import parse_workflow
from swarmify_workflows.ports import Storage
from swarmify_workflows.schedule import cron_problem, next_fire
from swarmify_workflows.sqlite import MIGRATIONS, SqliteStorage
from swarmify_workflows.starters import STARTERS, Starter
from swarmify_workflows.validate import validate

__all__ = [
    "MIGRATIONS",
    "STARTERS",
    "ConnectorRef",
    "ConnectorStage",
    "Draft",
    "GateStage",
    "Invalid",
    "SqliteStorage",
    "Stage",
    "Starter",
    "Storage",
    "SwarmStage",
    "Violation",
    "Workflow",
    "WorkflowModel",
    "WorkflowStage",
    "WorkflowVersion",
    "Workflows",
    "cron_problem",
    "dump_workflow",
    "next_fire",
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
        self,
        storage: Storage,
        connectors_for: Callable[[str], Mapping[str, ConnectorRef]],
        on_saved: Callable[[WorkflowVersion], None] = lambda _: None,
    ) -> None:
        """`on_saved` sees every new version once it is stored: the composition root uses it to
        hand the version's `schedule` to the Runs context, which owns the cron."""
        self.storage = storage
        self.connectors_for = connectors_for
        self.on_saved = on_saved

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
        refs = self.connectors_for(world_id)
        packages = {
            n: v for n in sorted(calls(draft.model)) if n in refs and (v := refs[n].version)
        }
        if workflow_id is None:
            workflow = Workflow(
                new_id("wf"), world_id, draft.model.name, 1, datetime.now(UTC).isoformat()
            )
            version = self.storage.add_workflow(
                workflow, yaml, _saved(workflow.id, world_id, draft.model.name, 1), packages
            )
        else:
            existing = self.storage.get_workflow(workflow_id)
            if existing is None or existing.world_id != world_id:
                raise KeyError(workflow_id)
            number = existing.current_version + 1
            event = _saved(workflow_id, world_id, draft.model.name, number)
            added = self.storage.add_version(workflow_id, yaml, draft.model.name, event, packages)
            if added is None:
                raise KeyError(workflow_id)
            version = added
        self.on_saved(version)
        return version

    def copy(self, workflow_id: str, world_id: str) -> WorkflowVersion:
        """A new workflow in `world_id` from this one's current YAML. Raises KeyError for an
        unknown workflow and Invalid when the other world lacks a connector it needs."""
        workflow = self.get(workflow_id)
        current = self.version(workflow_id, workflow.current_version) if workflow else None
        if current is None:
            raise KeyError(workflow_id)
        return self.save(world_id, current.yaml)

    def scheduled(self) -> dict[str, str]:
        """Every workflow with a schedule, across worlds, to its cron expression."""
        found: dict[str, str] = {}
        for workflow_id, yaml in self.storage.current_yaml().items():
            model, _ = parse_workflow(yaml)
            if model is not None and model.schedule:
                found[workflow_id] = model.schedule
        return found

    def list_all(self, world_id: str) -> list[Workflow]:
        return self.storage.list_workflows(world_id)

    def get(self, workflow_id: str) -> Workflow | None:
        return self.storage.get_workflow(workflow_id)

    def versions(self, workflow_id: str) -> list[WorkflowVersion]:
        return self.storage.list_versions(workflow_id)

    def version(self, workflow_id: str, number: int) -> WorkflowVersion | None:
        return self.storage.get_version(workflow_id, number)

    def stamp(self, versions_for: Callable[[str], Mapping[str, str]]) -> int:
        """Record, in every saved version, the package version `versions_for(world id)` gives
        for each package it calls but has not recorded, as a version saved before versions
        were recorded, or one calling a connector that has since become a package. Recorded
        versions never change. Returns how many versions it stamped."""
        stamped = 0
        for world_id, version in self.storage.all_versions():
            pins = versions_for(world_id)
            missing = {
                n: pins[n] for n in calls(version.model) - set(version.packages) if n in pins
            }
            if missing:
                self.storage.set_packages(version.id, {**version.packages, **missing})
                stamped += 1
        return stamped

    def using(self, package: str, version: str) -> list[tuple[Workflow, int]]:
        """Each saved workflow version, as its workflow and number, that records `package` at
        `version`."""
        return [
            (workflow, v.number)
            for _, v in self.storage.all_versions()
            if v.packages.get(package) == version and (workflow := self.get(v.workflow_id))
        ]

    def delete_world(self, world_id: str) -> None:
        """Drop a deleted world's workflows so they stop shaping cycle and depth checks."""
        self.storage.delete_world(world_id)

    def _references(self) -> dict[str, list[str]]:
        # ponytail: re-parses every current version on each save, outside the save's
        # transaction, so two concurrent saves (UI tab plus MCP) could each pass and together
        # make a cycle or nest past two, and the event's version number is read before the
        # UPDATE that assigns it. Fine for one operator; compute references and the number
        # inside the BEGIN IMMEDIATE transaction when a second writer appears.
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
