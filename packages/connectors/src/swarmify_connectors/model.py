"""The Connectors language: a Connector, the Tools it exposes, and one connector stage Call."""

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["tool", "stage", "publish"]
KINDS: tuple[Kind, ...] = ("tool", "stage", "publish")
STAGE_TYPES = ("swarm", "connector")
CallStatus = Literal["running", "polling", "done", "failed"]


@dataclass(frozen=True)
class Connector:
    """An MCP server (a command or a URL), the credential names it needs, and the stage types
    allowed to use it. Built-ins have no world and, for web search and the graph, no server:
    the agent runtime and Memory provide them."""

    id: str
    world_id: str | None
    name: str
    kind: Kind
    command: list[str] = field(default_factory=list)
    url: str | None = None
    credentials: list[str] = field(default_factory=list)
    stage_types: list[str] = field(default_factory=list)
    builtin: bool = False
    about: str = ""
    created_at: str | None = None

    @property
    def has_server(self) -> bool:
        return bool(self.command or self.url)


@dataclass(frozen=True)
class Tool:
    """One callable capability a connector exposes."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Call:
    """One connector stage call, keyed by the stage id. `response` is the latest raw response:
    the tool's result, or for a long job the last poll. `visit` is the run's send-back count
    when it was made; a later visit to the stage replaces it."""

    idempotency_key: str
    world_id: str
    run_id: str
    connector: str
    tool: str
    request: dict[str, Any]
    status: CallStatus
    started_at: str
    updated_at: str
    response: dict[str, Any] | None = None
    error: str | None = None
    job_id: str | None = None
    poll_tool: str | None = None
    progress: float | None = None
    message: str | None = None
    attempts: int = 0
    polls: int = 0
    visit: int = 0


@dataclass(frozen=True)
class KeyCheck:
    """What a live check of a built-in's saved keys found: the account they sign in to, or why
    they did not work."""

    ok: bool
    message: str


class ConnectorError(RuntimeError):
    """A connector could not start, refused a call, or reported a failed job."""
