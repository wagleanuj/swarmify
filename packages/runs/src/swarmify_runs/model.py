"""The Runs language: a Run of a pinned workflow version, its Stages in order, and the
Decisions people made at its gates."""

from dataclasses import dataclass, field
from typing import Any

from swarmify_runs.machine import RunStatus, StageStatus


@dataclass(frozen=True)
class StageDef:
    """A stage as the pinned workflow version defines it. `config` is every other field."""

    name: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunLink:
    """Another run, as a link to it: runs may cross worlds, so the world comes along."""

    id: str
    world_id: str
    name: str


@dataclass(frozen=True)
class Pinned:
    """A workflow version as Runs needs it to start a run of it."""

    world_id: str
    workflow_id: str
    version: int
    name: str
    stages: list[StageDef]
    budget_usd: float | None = None
    """The workflow's default Budget."""


@dataclass(frozen=True)
class Decision:
    """What a person decided at a gate. `path` is the decision artifact on disk (ADR 0001)."""

    id: str
    stage_index: int
    stage_name: str
    kind: str
    choice: str | None
    note: str
    send_back_to: str | None
    path: str
    decided_at: str


@dataclass(frozen=True)
class RunStageView:
    """A stage of a run as the timeline shows it. `id` is the connector idempotency key.

    `notes` are the send-back notes addressed to this stage: its context when it runs again.
    """

    id: str
    index: int
    name: str
    type: str
    status: StageStatus
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    child: RunLink | None = None
    """The child run a workflow stage started, while that visit to the stage lasts."""


@dataclass(frozen=True)
class Run:
    id: str
    world_id: str
    workflow_id: str
    workflow_version: int
    name: str
    status: RunStatus
    created_at: str
    updated_at: str
    stages: list[RunStageView]
    send_backs: int = 0
    parent_run_id: str | None = None
    decisions: list[Decision] = field(default_factory=list)
    parent: RunLink | None = None
    forked_from: RunLink | None = None
    """The run this one was forked from; `fork_stage_index` is the first stage that runs anew."""
    fork_stage_index: int | None = None
    forks: list[RunLink] = field(default_factory=list)
    promoted_from: str | None = None
    """The Library artifact (a path in the world folder) a promoted run started from."""
    budget_usd: float | None = None
    """The Budget, on the top run only: a child run spends its parent's (ADR 0013)."""
    spent_usd: float = 0.0
    """Estimated agent cost of this run and every run below it. An estimate, not billing."""
    over_budget: bool = False
    """The Budget is spent and the waiting stage is a budget gate: add money or abort."""
    baseline: bool = False
    """Baseline mode: one agent and no graph, to measure the swarm against."""


@dataclass(frozen=True)
class PublishPreview:
    """What the publish stage right after a gate will post, as the gate shows it (ADR 0007).
    `schedule` is a UTC ISO time, or None for now; `problems` would stop the post."""

    connector: str
    title: str
    visibility: str
    schedule: str | None = None
    description: str = ""
    video: str | None = None
    platforms: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Gate:
    """An open gate as the inbox lists it: which run waits, what it asks, how to answer."""

    run_id: str
    world_id: str
    run_name: str
    stage_id: str
    stage_index: int
    stage_name: str
    kind: str
    ask: str
    opened_at: str
    send_backs_left: int
    earlier_stages: list[str]
    parent_run_id: str | None = None
    parent: RunLink | None = None
    publish: PublishPreview | None = None
    """Set when the next stage publishes: exactly what confirming sends out."""
    budget_usd: float | None = None
    """For a `budget` gate: the Budget that was spent, and the spend so far."""
    spent_usd: float | None = None
