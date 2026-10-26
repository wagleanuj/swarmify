"""The Runs language: a Run of a pinned workflow version and its Stages, in order."""

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
class RunStageView:
    """A stage of a run as the timeline shows it. `id` is the connector idempotency key."""

    id: str
    index: int
    name: str
    type: str
    status: StageStatus
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


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
