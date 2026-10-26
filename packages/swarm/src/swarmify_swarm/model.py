"""The Swarm language for the whiteboard: Tasks of one swarm stage, their Claims and notes."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

TaskStatus = Literal["open", "claimed", "done"]
HEARTBEAT_TIMEOUT = timedelta(minutes=2)
"""A claimed task whose last heartbeat is older than this returns to open."""
OPERATOR = "operator"
"""`added_by` of a task a person added by hand."""


@dataclass(frozen=True)
class Note:
    author: str
    body: str
    ts: str


@dataclass(frozen=True)
class Task:
    id: str
    world_id: str
    run_id: str
    stage_id: str
    title: str
    body: str
    status: TaskStatus
    owner: str | None
    added_by: str
    heartbeat_at: str | None
    created_at: str
    updated_at: str
    depends_on: list[str] = field(default_factory=list)
    """Tasks that must be done before this one can be claimed (beads' `blocks`)."""
    notes: list[Note] = field(default_factory=list)


class Refused(ValueError):
    """The whiteboard refused the action, for example a claim on a task someone else holds."""


AgentRole = Literal["worker"]
AgentStatus = Literal["working", "done", "failed"]
LogKind = Literal["session", "tool", "denied", "result", "error"]


@dataclass(frozen=True)
class Agent:
    """One agent of a swarm stage. `session_id` is its Agent SDK session, so a replacement agent
    resumes it instead of starting over. `cost_usd` is the runtime's estimate, not billing."""

    id: str
    world_id: str
    run_id: str
    stage_id: str
    name: str
    role: AgentRole
    status: AgentStatus
    session_id: str | None
    cost_usd: float
    created_at: str
    updated_at: str
    skills: list[str] = field(default_factory=list)
    """What the session's init message said loaded from the world's skills plugin."""


@dataclass(frozen=True)
class LogEntry:
    """One line of a stage's live log: a session starting or ending, or one tool call the hooks
    saw, allowed (`tool`) or refused (`denied`, with the reason in `detail`)."""

    seq: int
    agent: str
    stage_id: str
    kind: LogKind
    ts: str
    tool: str | None = None
    input: dict[str, Any] | None = None
    detail: str = ""
