"""Ports of the Swarm context (ADR 0012): Storage and AgentRuntime.

Storage: SQLite is the adapter, Postgres the seam. Every write records an event carrying the
whole changed row in the same transaction (`task.updated`, `agent.updated`, `agent.logged`).
Unknown ids raise KeyError; refused actions raise Refused.

AgentRuntime: the Agent SDK is the adapter (`SdkRuntime`); tests hand it a scripted transport in
the SDK's own test style, so the real hook and in-process tool paths run without a model.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from swarmify_swarm.model import Agent, AgentRole, AgentStatus, LogEntry, LogKind, Task


class Storage(Protocol):
    def add_task(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        title: str,
        *,
        added_by: str,
        body: str = "",
        depends_on: tuple[str, ...] = (),
    ) -> Task:
        """`depends_on` must name tasks of the same stage."""
        ...

    def get_task(self, task_id: str) -> Task | None: ...

    def list_tasks(self, stage_id: str) -> list[Task]:
        """Oldest first."""
        ...

    def claim(self, task_id: str, agent: str, now: datetime | None = None) -> Task:
        """One immediate transaction: exactly one of any number of concurrent claims wins.

        Refused when the task is not open or a task it depends on is not done.
        """
        ...

    def heartbeat(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task:
        """Refresh the claim of `agent`, attaching `note` (partial progress) when given."""
        ...

    def finish(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task: ...

    def expire(self, now: datetime | None = None) -> list[Task]:
        """Return to open every claimed task whose heartbeat is older than HEARTBEAT_TIMEOUT,
        keeping its notes. Returns the tasks it reopened."""
        ...

    def delete_world(self, world_id: str) -> None: ...

    def agent(self, world_id: str, run_id: str, stage_id: str, name: str, role: AgentRole) -> Agent:
        """The stage's agent of that name, added the first time it is asked for."""
        ...

    def update_agent(
        self,
        stage_id: str,
        name: str,
        *,
        status: AgentStatus | None = None,
        session_id: str | None = None,
        cost_usd: float | None = None,
        skills: list[str] | None = None,
    ) -> Agent:
        """Change only the fields given. `cost_usd` is added to the agent's cost, so every
        session's estimate counts, a failed one's too."""
        ...

    def list_agents(self, run_id: str) -> list[Agent]:
        """Every agent of the run, oldest first."""
        ...

    def log(
        self,
        stage_id: str,
        agent: str,
        kind: LogKind,
        detail: str = "",
        tool: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> LogEntry:
        """Append one line to the stage's live log for `agent`, which must exist."""
        ...

    def list_log(self, stage_id: str) -> list[LogEntry]:
        """Oldest first."""
        ...


TOOL_SERVER = "swarmify"
"""The in-process tool server. Hooks and the allowlist see its tools as `mcp__swarmify__<name>`,
the MCP naming every Agent SDK session uses."""


def tool_id(name: str) -> str:
    return f"mcp__{TOOL_SERVER}__{name}"


class ToolError(Exception):
    """An in-process tool refused its call; the agent sees the message as an error result."""


@dataclass(frozen=True)
class AgentTool:
    """An in-process tool. `run` gets the call's arguments and answers text for the agent."""

    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class SessionSpec:
    """One agent session. `plugin` is the world folder, loaded as a local plugin with host
    settings off (ADR 0016); `resume` is the session id a replacement agent continues.
    `allowed` lists the tools the stage allows: exact names, or a `prefix*`."""

    system_prompt: str
    prompt: str
    cwd: Path
    plugin: Path
    tools: list[AgentTool]
    allowed: list[str]
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """External MCP servers by name, in the standard MCP config shape (the stage's connectors)."""
    resume: str | None = None
    max_turns: int | None = None


@dataclass(frozen=True)
class SessionInit:
    """What the session's init message reported."""

    session_id: str
    skills: list[str]
    plugin_errors: list[str]


@dataclass(frozen=True)
class SessionEnd:
    session_id: str
    cost_usd: float
    """The runtime's whole-session estimate, not billing."""
    is_error: bool
    result: str = ""


class SessionHooks(Protocol):
    """What the runtime calls back while a session runs."""

    def started(self, init: SessionInit) -> None: ...

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        """Every tool call, before it runs. A returned reason denies the call."""
        ...

    def cost(self, usd: float) -> None:
        """The session's estimated cost so far, after every result."""
        ...


class AgentRuntime(Protocol):
    async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
        """Run one session to its result. Raises when the session dies before one."""
        ...
