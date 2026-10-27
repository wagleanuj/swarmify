from dataclasses import dataclass, field

RETENTION_DAYS = 90
"""How long a new world keeps run binaries. 0 keeps them forever."""
RUN_CAP = 2
"""How many runs of a new world execute at once."""
AGENT_COUNT = 4
"""How many workers a new world's cast has, besides its planner and judge."""


@dataclass(frozen=True)
class World:
    id: str
    name: str
    purpose: str
    created_at: str
    retention_days: int = RETENTION_DAYS
    monthly_budget_usd: float | None = None
    """The world's spend ceiling per calendar month across all its runs. None: no ceiling."""
    run_cap: int = RUN_CAP
    agent_count: int = AGENT_COUNT


@dataclass(frozen=True)
class Record:
    """An agent's track record, folded from the world's event stream."""

    tasks_done: int = 0
    judged: int = 0
    """Stages the agent finished a task in that the judge gave a first verdict on."""
    first_try: int = 0
    """Of those, how many the judge passed on the first verdict."""
    send_backs: int = 0
    """Send-backs to stages the agent finished a task in."""
    cost_usd: float = 0.0
    """The runtime's estimate across every session, not billing."""


@dataclass(frozen=True)
class CastEdit:
    """One stored version of an agent's prompt and tools. Version 1 is never stored."""

    name: str
    number: int
    prompt: str
    tools: list[str] | None
    created_at: str


@dataclass(frozen=True)
class CastMember:
    """An agent of the world's cast at its current version, with its record."""

    name: str
    role: str
    version: int
    prompt: str
    tools: list[str] | None
    """Stage tool names the agent may use; None: any tool the stage allows."""
    updated_at: str | None
    record: Record = field(default_factory=Record)
