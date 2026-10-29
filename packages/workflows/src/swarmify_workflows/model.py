"""The Workflows language: Workflow, Workflow Version, the four Stage types, and a Violation."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

STAGE_TYPES = ("swarm", "gate", "connector", "workflow")
GATE_KINDS = ("pick", "approve", "confirm")


@dataclass(frozen=True)
class SwarmStage:
    """Agents pursue `goal` through a whiteboard until the judge passes `done_when`."""

    name: str
    goal: str
    done_when: str
    tools: list[str] = field(default_factory=list)
    graphs: list[str] = field(default_factory=list)
    output_type: str | None = None
    produces: list[str] = field(default_factory=list)
    model: str | None = None
    """The model the stage's agents run on, over the world's and the Cast's choice."""
    type: Literal["swarm"] = "swarm"


@dataclass(frozen=True)
class GateStage:
    """The run stops until a person answers `ask`: pick one, approve or send back, confirm."""

    name: str
    ask: str
    kind: str = "approve"
    max_send_backs: int | None = None
    """How often an approve gate may send the run back (None: twice); the next is refused."""
    type: Literal["gate"] = "gate"


@dataclass(frozen=True)
class ConnectorStage:
    """One idempotent call to connector `calls`, from `input` artifact (or several, `inputs`) to
    `output` artifact."""

    name: str
    calls: str
    input: str | None = None
    output: str | None = None
    params: dict[str, object] = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    type: Literal["connector"] = "connector"


@dataclass(frozen=True)
class WorkflowStage:
    """Runs workflow `runs` as a child run and takes the named artifacts from it."""

    name: str
    runs: str
    take: list[str] = field(default_factory=list)
    type: Literal["workflow"] = "workflow"


Stage = SwarmStage | GateStage | ConnectorStage | WorkflowStage


@dataclass(frozen=True)
class WorkflowModel:
    name: str
    stages: list[Stage]
    schedule: str | None = None
    budget_usd: float | None = None
    agents: int | None = None


@dataclass(frozen=True)
class Violation:
    """One named rule the workflow breaks. `stage` names the offending stage when there is one."""

    rule: str
    message: str
    stage: str | None = None


@dataclass(frozen=True)
class ConnectorRef:
    """What Workflows needs to know about a connector: its kind, the stage types it allows and
    `problems`, the connector's own check of a connector stage's config (calls, input, inputs,
    output, params), which names every problem that would stop the call."""

    name: str
    kind: str  # tool, stage or publish
    stage_types: frozenset[str]
    problems: Callable[[dict[str, Any]], list[str]] = field(
        default=lambda _: [], compare=False, repr=False
    )


@dataclass(frozen=True)
class Workflow:
    id: str
    world_id: str
    name: str
    current_version: int
    created_at: str


@dataclass(frozen=True)
class WorkflowVersion:
    """An immutable snapshot. Runs pin the version they started with."""

    id: str
    workflow_id: str
    number: int
    yaml: str
    created_at: str
    model: WorkflowModel
