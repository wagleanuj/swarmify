"""The Learning language: Skill versions, the Retro's Skill Proposal and Workflow Suggestion, and
the Scorecard's per-run scores."""

import re
from dataclasses import dataclass, field
from typing import Literal

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
"""The Agent Skills spec: 1 to 64 lowercase letters, digits and hyphens, matching the folder."""
ACTIONS = ("new", "changed", "retired")
Source = Literal["world", "shared"]
Status = Literal["pending", "approved", "rejected"]


@dataclass(frozen=True)
class SkillVersion:
    """One stored version of a world's skill. `run_id` is the run whose retro produced it, or
    the run an artifact was promoted from; None for a hand-written one."""

    id: str
    world_id: str
    name: str
    number: int
    content: str
    run_id: str | None
    note: str
    retired: bool
    created_at: str


@dataclass(frozen=True)
class Skill:
    """A skill as agents of the world load it: the world's own, or a shared one the world has
    not overridden (world wins on conflict)."""

    name: str
    description: str
    source: Source
    path: str
    """The SKILL.md, relative to the data dir."""
    content: str
    version: int | None = None
    """The world's current version number; None for a shared or hand-written skill."""
    run_id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class SkillChange:
    """A Retro's Skill Proposal: one new, changed or retired skill, with the evidence for it."""

    action: str
    name: str
    content: str
    summary: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowSuggestion:
    """A Retro's Workflow Suggestion: the workflow YAML as a new version, drawn from a manual
    intervention that recurred across runs."""

    summary: str
    yaml: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Proposal:
    """What a Retro proposed for one run, awaiting its gate or decided at it."""

    id: str
    world_id: str
    run_id: str
    stage_id: str
    skill: SkillChange | None
    workflow: WorkflowSuggestion | None
    status: Status
    note: str
    path: str
    """The proposal artifact, relative to the world folder."""
    created_at: str
    decided_at: str | None = None


@dataclass(frozen=True)
class RunScore:
    """One run on the Scorecard. `cost_usd` is the runtime's estimate, not billing."""

    run_id: str
    name: str
    workflow_version: int | None
    baseline: bool
    status: str
    created_at: str
    judged: int
    """Swarm stages the judge gave a first verdict on."""
    first_try: int
    """Of those, passed on the first verdict."""
    send_backs: int
    cost_usd: float
    duration_s: float | None
    outcomes: dict[str, float]
    """The published artifacts' latest Outcomes by each one's Primary Metric, summed per metric,
    as {"views": 9000, "clicks": 12}; empty when nothing was measured."""
