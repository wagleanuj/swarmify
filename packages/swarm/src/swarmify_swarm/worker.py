"""One Worker working a swarm stage's whiteboard through the AgentRuntime port.

The worker's session gets the in-process tools below, the stage's allowlist and the world's
skills. Its hooks write every tool call to the stage's live log, deny tools outside the
allowlist and a claim before the agent's first graph query, and keep the agent's session id and
estimated cost. A session that dies is replaced by one that resumes the stored session id.

Memory and the graph are other contexts, so the composition root hands in `Files` and `Graph`.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from swarmify_swarm.model import Refused, Task
from swarmify_swarm.ports import (
    AgentRuntime,
    AgentTool,
    SessionInit,
    SessionSpec,
    Storage,
    ToolError,
    tool_id,
)

WORKER = "worker-1"
SWARM = "swarmify"
"""`added_by` of the task the swarm seeds from the stage goal."""
SESSIONS = 2
"""The worker's first session and one replacement."""
# ponytail: a fixed turn cap until the Budget (T15) stops sessions by cost.
MAX_TURNS = 200

GRAPH, CLAIM = tool_id("query_graph"), tool_id("claim_task")
BUILTIN = ["Skill", "Read"]
"""Built-ins every worker may use. Read is limited to the world folder by the hooks."""
STAGE_TOOLS = {"web_search": ["WebSearch", "WebFetch"], "graph": []}
"""A stage's `tools` beyond the built-ins name connectors, allowed as `mcp__<name>__*`."""

Graph = Callable[[str, str], str]
"""(world id, question) to what the world graph knows."""
Servers = Callable[[str, list[str]], dict[str, dict[str, Any]]]
"""(world id, connector names) to their MCP server configs, for the session."""


class Files(Protocol):
    """What the swarm needs from Memory: notes and artifacts as files in the run folder."""

    def stage_folder(self, world_id: str, run_id: str, stage_id: str) -> Path: ...

    def artifacts(self, world_id: str, run_id: str) -> list[str]:
        """Artifacts of the run so far, as paths relative to the run folder."""
        ...

    def write_note(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
        title: str,
        body: str,
        task_id: str | None = None,
    ) -> str:
        """Returns the note's path relative to the world folder."""
        ...

    def write_artifact(
        self, world_id: str, run_id: str, stage_id: str, author: str, name: str, content: str
    ) -> str: ...


class StageError(RuntimeError):
    """The swarm stage failed; the message lands on the stage."""


def no_graph(_world_id: str, _question: str) -> str:
    return "The world graph has nothing yet. Work from the task and the stage goal."


def no_servers(_world_id: str, _names: list[str]) -> dict[str, dict[str, Any]]:
    return {}


def allowlist(stage_tools: list[str]) -> list[str]:
    allowed = [tool_id(t) for t in TOOLS] + BUILTIN
    for name in stage_tools:
        allowed += STAGE_TOOLS.get(name, [f"mcp__{name}__*"])
    return allowed


def is_allowed(tool: str, allowed: list[str]) -> bool:
    return any(tool == a or (a.endswith("*") and tool.startswith(a[:-1])) for a in allowed)


def _obj(*required: str, **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


def _str(about: str) -> dict[str, Any]:
    return {"type": "string", "description": about}


TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "query_graph": (
        "Ask the world graph what is already known. Required before your first claim.",
        _obj("question", question=_str("What you need to know")),
    ),
    "list_tasks": ("The stage's whiteboard: every task with its status and notes.", _obj()),
    "claim_task": (
        "Claim a task: the one named, or else the oldest ready open task.",
        _obj(task_id=_str("A task id; omit it to take the next ready task")),
    ),
    "heartbeat": (
        "Keep your claim alive (at least every minute), optionally with partial notes.",
        _obj("task_id", task_id=_str("The task you hold"), note=_str("Progress so far")),
    ),
    "finish_task": (
        "Mark a task you hold done, with a short closing note.",
        _obj("task_id", task_id=_str("The task you hold"), note=_str("What you did")),
    ),
    "add_task": (
        "Add a task you discovered to the whiteboard.",
        _obj(
            "title",
            title=_str("The task"),
            body=_str("Details"),
            depends_on={"type": "array", "items": {"type": "string"}, "description": "Task ids"},
        ),
    ),
    "write_note": (
        "Write what you learned as a markdown note in the run folder.",
        _obj(
            "title",
            "body",
            title=_str("Note title"),
            body=_str("Markdown body"),
            task_id=_str("The task it belongs to"),
        ),
    ),
    "write_artifact": (
        "Write an output file of this stage, for example ideas.md.",
        _obj("name", "content", name=_str("A plain file name"), content=_str("The file's text")),
    ),
}


class Swarm:
    def __init__(
        self,
        board: Storage,
        runtime: AgentRuntime,
        files: Files,
        graph: Graph = no_graph,
        servers: Servers = no_servers,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.board = board
        self.runtime = runtime
        self.files = files
        self.graph = graph
        self.servers = servers
        self.clock = clock

    def run_stage(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        stage_name: str,
        config: dict[str, Any],
        world_folder: Path,
    ) -> None:
        """Run the stage's worker until its session ends. Raises StageError when every
        session failed. A worker that already finished (a retried step) does not run again."""
        goal = str(config.get("goal", "")).strip()
        if not self.board.list_tasks(stage_id):
            # ponytail: one seed task from the goal until the Planner (T08) seeds the board.
            self.board.add_task(world_id, run_id, stage_id, goal or stage_name, added_by=SWARM)
        board = self.board
        if board.agent(world_id, run_id, stage_id, WORKER, "worker").status == "done":
            return
        stage_tools = [str(t) for t in config.get("tools") or []]
        allowed = allowlist(stage_tools)
        hooks = _Hooks(board, stage_id, WORKER, allowed, world_folder)
        for _ in range(SESSIONS):
            resume = board.agent(world_id, run_id, stage_id, WORKER, "worker").session_id
            if resume:
                board.log(stage_id, WORKER, "session", f"A replacement resumes session {resume}.")
            spec = SessionSpec(
                system_prompt=self._system_prompt(world_id, run_id, stage_name, config),
                prompt=f"Work the whiteboard of stage {stage_name} until no task is ready.",
                cwd=self.files.stage_folder(world_id, run_id, stage_id),
                plugin=world_folder,
                tools=self._tools(world_id, run_id, stage_id, WORKER),
                allowed=allowed,
                servers=self.servers(world_id, [t for t in stage_tools if t not in STAGE_TOOLS]),
                resume=resume,
                max_turns=MAX_TURNS,
            )
            try:
                end = asyncio.run(self.runtime.run(spec, hooks))
            except Exception as exc:
                board.log(stage_id, WORKER, "error", f"The session died: {exc}")
                continue
            if end.is_error:
                board.log(stage_id, WORKER, "error", f"The session failed: {end.result}")
                continue
            board.log(
                stage_id,
                WORKER,
                "result",
                f"Session ended at an estimated ${end.cost_usd:.4f}. {end.result}".strip(),
            )
            board.update_agent(stage_id, WORKER, status="done")
            return
        board.update_agent(stage_id, WORKER, status="failed")
        raise StageError(f"{WORKER} failed {SESSIONS} sessions in a row; see the stage log")

    def _system_prompt(
        self, world_id: str, run_id: str, stage_name: str, config: dict[str, Any]
    ) -> str:
        produces = ", ".join(str(p) for p in config.get("produces") or [])
        prior = self.files.artifacts(world_id, run_id)
        return "\n".join(
            [
                f"You are {WORKER}, a worker in a swarm. Stage: {stage_name}.",
                f"Goal: {config.get('goal', '')}",
                f"Done when: {config.get('done_when', '')}",
                f"Write these artifacts: {produces}." if produces else "",
                f"Artifacts of earlier stages: {', '.join(prior)}." if prior else "",
                "How you work: call query_graph before your first claim and reuse what the world"
                " knows. claim_task takes the next ready task. heartbeat at least every minute"
                " while you hold one. Write what you learn with write_note and outputs with"
                " write_artifact. finish_task with a short note. add_task for work you discover."
                " Stop when no task is ready.",
            ]
        )

    def _tools(self, world_id: str, run_id: str, stage_id: str, agent: str) -> list[AgentTool]:
        board, files, now = self.board, self.files, self.clock

        def task_text(task: Task) -> str:
            return json.dumps(asdict(task))

        def own(task_id: Any) -> str:
            """A task id the agent names must be on this stage's whiteboard, never another's."""
            task = board.get_task(str(task_id))
            if task is None or task.stage_id != stage_id:
                raise ToolError("No such task on this whiteboard")
            return task.id

        def claim(args: dict[str, Any]) -> str:
            if args.get("task_id"):
                return task_text(board.claim(own(args["task_id"]), agent, now()))
            for task in board.list_tasks(stage_id):
                if task.status == "open":
                    try:
                        return task_text(board.claim(task.id, agent, now()))
                    except Refused:
                        continue
            raise ToolError("No task is ready to claim. Add one with add_task, or stop.")

        handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "query_graph": lambda a: self.graph(world_id, str(a.get("question", ""))),
            "list_tasks": lambda a: json.dumps([asdict(t) for t in board.list_tasks(stage_id)]),
            "claim_task": claim,
            "heartbeat": lambda a: task_text(
                board.heartbeat(own(a.get("task_id")), agent, a.get("note"), now())
            ),
            "finish_task": lambda a: task_text(
                board.finish(own(a.get("task_id")), agent, a.get("note"), now())
            ),
            "add_task": lambda a: task_text(
                board.add_task(
                    world_id,
                    run_id,
                    stage_id,
                    str(a.get("title", "")),
                    added_by=agent,
                    body=str(a.get("body", "")),
                    depends_on=tuple(str(d) for d in a.get("depends_on") or []),
                )
            ),
            "write_note": lambda a: (
                "Wrote "
                + files.write_note(
                    world_id,
                    run_id,
                    stage_id,
                    agent,
                    str(a.get("title", "")),
                    str(a.get("body", "")),
                    str(a["task_id"]) if a.get("task_id") else None,
                )
            ),
            "write_artifact": lambda a: (
                "Wrote "
                + files.write_artifact(
                    world_id,
                    run_id,
                    stage_id,
                    agent,
                    str(a.get("name", "")),
                    str(a.get("content", "")),
                )
            ),
        }

        def bind(fn: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], Any]:
            async def run(args: dict[str, Any]) -> str:
                try:
                    return fn(args)
                except (Refused, ValueError) as exc:
                    raise ToolError(str(exc)) from exc

            return run

        return [
            AgentTool(name, about, schema, bind(handlers[name]))
            for name, (about, schema) in TOOLS.items()
        ]


class _Hooks:
    """The SessionHooks of one agent: the live log, the allowlist and the graph-first rule."""

    def __init__(
        self, board: Storage, stage_id: str, agent: str, allowed: list[str], world_folder: Path
    ) -> None:
        self.board = board
        self.stage_id = stage_id
        self.agent = agent
        self.allowed = allowed
        self.world_folder = world_folder.resolve()

    def started(self, init: SessionInit) -> None:
        self.board.update_agent(
            self.stage_id, self.agent, session_id=init.session_id, skills=init.skills
        )
        detail = (
            f"Session {init.session_id} started with skills: {', '.join(init.skills) or 'none'}."
        )
        if init.plugin_errors:
            detail += f" The skills plugin reported: {'; '.join(init.plugin_errors)}."
        self.board.log(
            self.stage_id, self.agent, "error" if init.plugin_errors else "session", detail
        )

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        reason = self._refusal(tool, input)
        kind = "denied" if reason else "tool"
        self.board.log(self.stage_id, self.agent, kind, reason or "", tool=tool, input=input)
        return reason

    def cost(self, usd: float) -> None:
        # ponytail: cost lands once per session result, not per tool call (the SDK reports
        # total_cost_usd only on the result), so the Budget (T15) can stop a session no sooner.
        self.board.update_agent(self.stage_id, self.agent, cost_usd=usd)

    def _refusal(self, tool: str, input: dict[str, Any]) -> str | None:
        if not is_allowed(tool, self.allowed):
            return f"{tool} is not allowed in this stage."
        if tool == "Read":
            path = Path(str(input.get("file_path", "")))
            if not path.is_absolute() or not path.resolve().is_relative_to(self.world_folder):
                return "Read is limited to this world's folder."
        if tool == CLAIM and not any(
            e.agent == self.agent and e.kind == "tool" and e.tool == GRAPH
            for e in self.board.list_log(self.stage_id)
        ):
            return "Query the graph first: call query_graph about the work before your first claim."
        return None
