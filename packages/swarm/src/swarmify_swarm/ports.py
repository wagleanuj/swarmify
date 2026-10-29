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

from swarmify_swarm.model import Agent, AgentRole, AgentStatus, LogEntry, LogKind, Task, Verdict


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

    def add_tasks(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        tasks: list[tuple[str, str, tuple[int, ...]]],
        *,
        added_by: str,
    ) -> list[Task]:
        """Add (title, body, indexes of earlier tasks of the list it depends on) in one
        transaction, so a planner's seed lands whole or not at all."""
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

    def record_verdict(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        *,
        passed: bool,
        reasons: list[str],
        gaps: list[str],
        items: list[dict[str, Any]],
        path: str,
        added_by: str,
    ) -> Verdict:
        """The stage's next verdict (loop 1, 2, ...) and its gap tasks, added as open tasks by
        `added_by`, in one transaction. Appends `verdict.created` carrying the verdict."""
        ...

    def list_verdicts(self, stage_id: str) -> list[Verdict]:
        """Oldest loop first."""
        ...

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
        model: str | None = None,
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
    settings off (ADR 0016), or None for none; `resume` is the session id a replacement agent
    continues. `allowed` lists the tools the stage allows: exact names, or a `prefix*`."""

    system_prompt: str
    prompt: str
    cwd: Path
    plugin: Path | None
    tools: list[AgentTool]
    allowed: list[str]
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """External MCP servers by name, in the standard MCP config shape (the stage's connectors)."""
    resume: str | None = None
    max_turns: int | None = None
    output_schema: dict[str, Any] | None = None
    """A JSON schema the session's final answer must follow (structured output)."""
    model: str | None = None
    """The model to run on; None leaves it to the runtime's default."""
    sandbox: dict[str, Any] | None = None
    """The OS sandbox of the session's Bash, in the Agent SDK's `SandboxSettings` shape (plus
    the CLI's `filesystem` and `credentials` rules); None runs Bash, when allowed, unsandboxed."""
    env: dict[str, str] = field(default_factory=dict)
    """Over the inherited environment; an empty value blanks an inherited variable."""


@dataclass(frozen=True)
class SessionInit:
    """What the session's init message reported."""

    session_id: str
    skills: list[str]
    plugin_errors: list[str]
    model: str = ""
    """The model the session runs on."""


@dataclass(frozen=True)
class SessionEnd:
    session_id: str
    cost_usd: float
    """The runtime's whole-session estimate, not billing."""
    is_error: bool
    result: str = ""
    output: Any = None
    """The structured answer when the spec asked for one."""
    stopped: str | None = None
    """Why the hooks stopped the session before its result (the run's Budget is spent)."""


class SessionHooks(Protocol):
    """What the runtime calls back while a session runs."""

    def started(self, init: SessionInit) -> None: ...

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        """Every tool call, before it runs. A returned reason denies the call."""
        ...

    def cost(self, usd: float) -> str | None:
        """An increment of the session's estimated cost: one per model step, and at the result
        the rest, so the increments add up to the runtime's whole-session estimate. A returned
        reason stops the session (ignored at the result, when it has ended anyway)."""
        ...


class AgentRuntime(Protocol):
    async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
        """Run one session to its result. Raises when the session dies before one."""
        ...
