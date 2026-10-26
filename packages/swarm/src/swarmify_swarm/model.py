"""The Swarm language for the whiteboard: Tasks of one swarm stage, their Claims and notes."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

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
