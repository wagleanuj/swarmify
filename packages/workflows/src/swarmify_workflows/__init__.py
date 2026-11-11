"""Workflows context: a Workflow is a saved, versioned recipe of Stages.

Saving YAML parses it into the four stage types, runs every rule, and on success appends an
immutable Workflow Version. Connectors belong to another context, so the composition root hands
in a function from world id to the connectors that world allows.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from swarmify_kernel import Event, new_id
from swarmify_workflows.dump import dump_workflow, restage
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
from swarmify_workflows.validate import validate, warnings

__all__ = [
    "MIGRATIONS",
    "STARTERS",
    "ConnectorRef",
    "ConnectorStage",
    "Draft",
    "GateStage",
    "Invalid",
    "RunDraft",
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
    """A checked but unsaved workflow: its model when the shape parsed, every violation, and
    the warnings, which save but likely do not do what was meant."""

    model: WorkflowModel | None
    violations: list[Violation]
    warnings: list[Violation] = field(default_factory=list)


@dataclass(frozen=True)
class RunDraft:
    """A Run Change as the workflow's next version would take it: the current version's text
    `before` and the `yaml` with the change in it, the check's violations, and the `conflicts`,
    how the current version differs from the run's in the stages the change touches. `saved` is
    the new version once saved."""

    workflow_id: str
    version: int
    """The current version the change applies to."""
    run_version: int
    before: str
    yaml: str
    violations: list[Violation]
    conflicts: list[str]
    saved: WorkflowVersion | None = None


class Invalid(ValueError):
    def __init__(self, violations: list[Violation]) -> None:
        super().__init__("; ".join(v.message for v in violations))
        self.violations = violations


class Workflows:
    def __init__(
        self,
        storage: Storage,
        connectors_for: Callable[[str, Mapping[str, str]], Mapping[str, ConnectorRef]],
        on_saved: Callable[[WorkflowVersion], None] = lambda _: None,
    ) -> None:
        """`connectors_for(world id, pins)` gives the world's connectors, a package named in
        `pins` at that version over the world's pin. `on_saved` sees every new version once it
        is stored: the composition root uses it to hand the version's `schedule` to the Runs
        context, which owns the cron."""
        self.storage = storage
        self.connectors_for = connectors_for
        self.on_saved = on_saved

    def check(
        self,
        world_id: str,
        yaml: str,
        workflow_id: str | None = None,
        pins: Mapping[str, str] | None = None,
    ) -> Draft:
        """Parse and validate without saving. `workflow_id` is the workflow a new version is for;
        `pins`, the package versions a saved version recorded, checks it against those."""
        model, violations = parse_workflow(yaml)
        connectors = self.connectors_for(world_id, pins or {})
        if model is not None:
            violations += validate(
                model,
                workflow_id=workflow_id,
                connectors=connectors,
                references=self._references(),
            )
        return Draft(model, violations, warnings(model, connectors) if model is not None else [])

    def save(self, world_id: str, yaml: str, workflow_id: str | None = None) -> WorkflowVersion:
        """Create a workflow (no id) or append a version to one. Raises Invalid; saves nothing."""
        draft = self.check(world_id, yaml, workflow_id)
        if draft.violations or draft.model is None:
            raise Invalid(draft.violations)
        refs = self.connectors_for(world_id, {})
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

    def save_from_run(
        self,
        workflow_id: str,
        run_version: int,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
        *,
        save: bool = False,
        confirmed: bool = False,
    ) -> RunDraft:
        """A Run Change applied to the workflow's current version, matching stages by name: the
        stages `before`, each a `name`, `type` and fields as the run of `run_version` had them,
        give way to the stages `after`. A Stage Edit is one stage before and after, a Skip one
        before and none after, a Restructure the stages from its target on. With `save` it
        appends the draft as a new version, unless the check refuses it or it has conflicts not
        `confirmed`. Raises KeyError for an unknown workflow or version."""
        workflow = self.get(workflow_id)
        current = self.version(workflow_id, workflow.current_version) if workflow else None
        pinned = self.version(workflow_id, run_version)
        if workflow is None or current is None or pinned is None:
            raise KeyError(workflow_id)
        texts = restage(current.yaml, before, after)
        violations: list[Violation] = []
        if texts is None:
            name = before[0]["name"] if before else ""
            violations.append(
                Violation("run_change", f"v{current.number} has no stage named {name}", name)
            )
            texts = (current.yaml, current.yaml)
        elif texts[0] == texts[1]:
            msg = f"v{current.number} already has this change"
            violations.append(Violation("run_change", msg, before[0]["name"]))
        else:
            violations += self.check(workflow.world_id, texts[1], workflow_id).violations
        conflicts = [
            f"Since this run's v{pinned.number}, v{current.number} {what}."
            for s in before
            if (what := _changed(s["name"], pinned.model, current.model))
        ]
        draft = RunDraft(workflow_id, current.number, run_version, *texts, violations, conflicts)
        if save and not violations and (confirmed or not conflicts):
            return replace(draft, saved=self.save(workflow.world_id, draft.yaml, workflow_id))
        return draft

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


def _changed(name: str, was: WorkflowModel, now: WorkflowModel) -> str | None:
    """How stage `name` of `was` differs in `now`, None when it is the same or `was` lacks it."""
    old = next((asdict(s) for s in was.stages if s.name == name), None)
    new = next((asdict(s) for s in now.stages if s.name == name), None)
    if old is None or old == new:
        return None
    if new is None:
        return f"removed {name}"
    return f"changed {name} ({', '.join(k for k in new if new[k] != old.get(k))})"


def _saved(workflow_id: str, world_id: str, name: str, number: int) -> Event:
    return Event.new(
        "workflow.saved",
        world_id=world_id,
        payload={"workflow_id": workflow_id, "name": name, "version": number},
    )
