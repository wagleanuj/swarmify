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
    schedule: str | None = None
    """The version's cron, if any: a tick that fires after the schedule was dropped finds None."""


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
    artifact: str | None = None
    """The artifact the choice names, as a path in the world folder: the card picked at a pick
    gate, or what an approve gate approved (story 53)."""
    publish: dict[str, Any] | None = None
    """At a gate before a publish stage: every field's value as confirmed, by key, which is what
    the stage posts."""
    by: str | None = None
    """Who answered from outside the web UI (a Remote Approval), as its connector names them,
    such as "anuj in Slack"; None for an answer given in Swarmify."""


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
    calls: str | None = None
    """The connector a connector stage calls, so a skin can show what it made."""
    ask: str | None = None
    """What a gate asks, kept after it was answered."""
    inputs: list[str] = field(default_factory=list)
    """The artifact names a connector stage takes (its `input` or `inputs`), so a skin can show
    what went in."""


@dataclass(frozen=True)
class Run:
    id: str
    world_id: str
    workflow_id: str | None
    """None for an Ad Hoc Run: one started from a typed goal with no saved workflow."""
    workflow_version: int | None
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

    def send_back_note(self, stage_name: str) -> str:
        """The note of the send-back that caused this visit, when it named `stage_name`; empty
        when the stage reruns because the send-back named an earlier stage."""
        sent = next((d for d in reversed(self.decisions) if d.send_back_to), None)
        return sent.note if sent and sent.send_back_to == stage_name else ""


@dataclass(frozen=True)
class PublishField:
    """One value a publish connector will send, as the gate shows it. `editable` ones the person
    may change before confirming; `hint` is the connector's note on it, like when a schedule
    goes public."""

    key: str
    label: str
    value: Any = None
    editable: bool = False
    hint: str = ""


@dataclass(frozen=True)
class PublishPreview:
    """What the publish stage right after a gate will post, as its connector describes it (ADR
    0007): the fields it sends and the `problems` that would stop the post."""

    connector: str
    fields: list[PublishField] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def values(self) -> dict[str, Any]:
        return {f.key: f.value for f in self.fields}


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
    max_send_backs: int = 2
    """How many send-backs the gate allows in all, its workflow's `max_send_backs`."""
    publish: PublishPreview | None = None
    """Set when the next stage publishes: exactly what confirming sends out."""
    budget_usd: float | None = None
    """For a `budget` gate: the Budget that was spent, and the spend so far."""
    spent_usd: float | None = None
