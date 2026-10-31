"""The Connectors language: a Connector, the Tools it exposes, and one connector stage Call."""

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["tool", "stage", "publish", "notify"]
KINDS: tuple[Kind, ...] = ("tool", "stage", "publish", "notify")
"""A `notify` connector runs in no stage: it tells a person about gates, and may offer Remote
Approvals."""
STAGE_TYPES = ("swarm", "connector")
CallStatus = Literal["running", "polling", "done", "failed"]


@dataclass(frozen=True)
class Connector:
    """An MCP server (a command or a URL), the credential names it needs, and the stage types
    allowed to use it."""

    id: str
    world_id: str | None
    name: str
    kind: Kind
    command: list[str] = field(default_factory=list)
    url: str | None = None
    credentials: list[str] = field(default_factory=list)
    stage_types: list[str] = field(default_factory=list)
    about: str = ""
    created_at: str | None = None
    health: str | None = None
    """The tool that checks what the connector depends on, answering `checks`. A package's
    health also polls the Services it declares."""
    types: dict[str, Any] = field(default_factory=dict)
    """Named input types it declares, each a JSON Schema, as `script` (issue 133)."""
    input_types: list[str | None] = field(default_factory=list)
    """The declared type of a stage's `input`, or of each of its `inputs` in order; None or
    left out is untyped."""
    world_types: str | None = None
    """The tool that answers its types for one world (issue 140), given `world` and the world's
    `assets_path`, as `{"types": {name: schema}, "note": "..."}`; `types` stays the fallback."""
    manifest: dict[str, Any] = field(default_factory=dict, compare=False)
    """The Manifest that declares it (ADR 0019), which the planner describes it from; empty for
    a connector the add form drafted, not yet installed as a package."""
    version: str | None = None
    """The Connector Package version the world pins (ADR 0023); None for a connector the add
    form drafted, not yet installed as a package."""
    folder: str | None = None
    """Where that version is installed; its command starts there."""

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
class HealthCheck:
    """One thing a connector depends on, such as a model server, and whether it answers."""

    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class KeyCheck:
    """What a package's Setup test found: the account its values sign in to, or why they did not
    work."""

    ok: bool
    message: str


class ConnectorError(RuntimeError):
    """A connector could not start, refused a call, or reported a failed job."""
