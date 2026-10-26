"""One Worker working a swarm stage's whiteboard through the AgentRuntime port.

The worker's session gets the in-process tools below, the stage's allowlist and the world's
skills. Its hooks write every tool call to the stage's live log, deny tools outside the
allowlist and a claim before the agent's first graph query, and keep the agent's session id and
estimated cost. A session that dies is replaced by one that resumes the stored session id.

After every cost estimate the hooks ask the run's Budget (`budget`, from the composition root);
once it is spent they stop the session and the stage raises Stopped, leaving the agent's session
to resume when money is added. A baseline stage is one worker with no graph: no planner, no
judge, no graph tools and no graph-first rule, the single-agent measure the swarm is compared
against.

Memory and the graph are other contexts, so the composition root hands in `Files` and `Graphs`.
Graph tools are wrapped: an agent may name the graph of its own world or of a world the stage
lists under `graphs`, by id, name or folder, and nothing else (ADR 0009). Every answer is labelled
with the world it came from.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from swarmify_swarm.model import AgentRole, Refused, Task, Verdict
from swarmify_swarm.ports import (
    AgentRuntime,
    AgentTool,
    SessionInit,
    SessionSpec,
    Storage,
    ToolError,
    tool_id,
)
from swarmify_swarm.projection import (
    MAX_LOOPS,
    PLAN_SCHEMA,
    VERDICT_SCHEMA,
    Item,
    OutputType,
    Source,
    notes_for_judge,
    output_type,
    parse_plan,
    parse_verdict,
    project,
    verdict_file,
)

WORKER, PLANNER, JUDGE = "worker-1", "planner", "judge"
SESSIONS = 2
"""An agent's first session and one replacement."""
# ponytail: fixed turn caps as well, for runs with no Budget.
MAX_TURNS, SHORT_TURNS = 200, 30
SHORT_TOOLS = ("query_graph", "list_tasks")
"""What the planner and the judge may call besides the built-ins: they read, never claim."""
T = TypeVar("T")

GRAPH, CLAIM = tool_id("query_graph"), tool_id("claim_task")
BUILTIN = ["Skill", "Read"]
"""Built-ins every worker may use. Read is limited to the world folder by the hooks."""
STAGE_TOOLS = {"web_search": ["WebSearch", "WebFetch"], "graph": []}
"""A stage's `tools` beyond the built-ins name connectors, allowed as `mcp__<name>__*`."""

Servers = Callable[[str, list[str]], dict[str, dict[str, Any]]]
"""(world id, connector names) to their MCP server configs, for the session."""
Budget = Callable[[str], str | None]
"""(run id) to why its agents must stop, once the run's Budget is spent; None otherwise."""


class Files(Protocol):
    """What the swarm needs from Memory: notes and artifacts as files in the run folder."""

    def stage_folder(self, world_id: str, run_id: str, stage_id: str) -> Path: ...

    def artifacts(self, world_id: str, run_id: str) -> list[str]:
        """Artifacts of the run so far, as paths relative to the run folder."""
        ...

    def pinned(self, world_id: str) -> list[str]:
        """The world's pinned artifacts, required reading, as paths relative to its folder."""
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

    def notes(self, world_id: str, run_id: str, stage_id: str) -> list[tuple[str, str]]:
        """The stage's notes as (path relative to the world folder, body without frontmatter)."""
        ...

    def write_typed(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
        name: str,
        type: str,
        fields: dict[str, Any],
        body: str,
    ) -> str:
        """A typed artifact `name` (markdown) of `type` with `fields` in its frontmatter.
        Writing a name again replaces it. Returns the path relative to the world folder."""
        ...


class Graphs(Protocol):
    """What the swarm needs from Memory's GraphIndex: read-only graph tools per world folder."""

    def folder(self, world: str) -> Path | None:
        """The folder of the world named by id or name; None when there is no such world."""
        ...

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str: ...


class NoGraphs:
    def folder(self, world: str) -> Path | None:
        return None

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        return "The world graph has nothing yet. Work from the task and the stage goal."


class StageError(RuntimeError):
    """The swarm stage failed; the message lands on the stage."""


class Stopped(Exception):
    """The run's Budget stopped the stage's agents. Not a failure: the stage runs again, its
    agents resuming their sessions, once money is added."""


def no_budget(_run_id: str) -> str | None:
    return None


@dataclass(frozen=True)
class _At:
    """The stage being run."""

    world_id: str
    run_id: str
    stage_id: str
    stage_name: str
    config: dict[str, Any]
    world_folder: Path
    notes: tuple[str, ...] = ()
    """The send-back notes addressed to this stage, oldest first."""
    baseline: bool = False


def _wants(config: dict[str, Any]) -> str:
    produces = ", ".join(str(p) for p in config.get("produces") or [])
    out = f"Write these artifacts: {produces}." if produces else ""
    if config.get("output_type"):
        out += f" The judge projects the notes into output type {config['output_type']}."
    return out.strip()


def no_servers(_world_id: str, _names: list[str]) -> dict[str, dict[str, Any]]:
    return {}


def allowlist(stage_tools: list[str], baseline: bool = False) -> list[str]:
    allowed = [tool_id(t) for t in TOOLS if not (baseline and t in GRAPH_TOOLS)] + BUILTIN
    for name in stage_tools:
        allowed += STAGE_TOOLS.get(name, [f"mcp__{name}__*"])
    return allowed


def is_allowed(tool: str, allowed: list[str]) -> bool:
    return any(tool == a or (a.endswith("*") and tool.startswith(a[:-1])) for a in allowed)


def _obj(*required: str, **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


def _str(about: str) -> dict[str, Any]:
    return {"type": "string", "description": about}


_WORLD = _str("Whose graph: omit for this world's, or a world the stage lists in `graphs`")
GRAPH_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "query_graph": (
        "Ask the world graph what is already known. Required before your first claim.",
        _obj("question", question=_str("What you need to know"), world=_WORLD),
    ),
    "get_node": (
        "Everything the graph holds on one node.",
        _obj("label", label=_str("Node label or id"), world=_WORLD),
    ),
    "get_neighbors": (
        "A node's direct neighbours and how they connect.",
        _obj("label", label=_str("Node label or id"), world=_WORLD),
    ),
    "god_nodes": (
        "The most connected nodes: what the world's knowledge revolves around.",
        _obj(top_n={"type": "integer", "description": "How many, default 10"}, world=_WORLD),
    ),
    "shortest_path": (
        "How two concepts connect in the graph.",
        _obj("source", "target", source=_str("From"), target=_str("To"), world=_WORLD),
    ),
    "graph_stats": ("Size of the graph: nodes, edges, communities.", _obj(world=_WORLD)),
}
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    **GRAPH_TOOLS,
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
        graphs: Graphs | None = None,
        servers: Servers = no_servers,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        budget: Budget = no_budget,
    ) -> None:
        self.budget = budget
        self.board = board
        self.runtime = runtime
        self.files = files
        self.graphs = graphs or NoGraphs()
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
        notes: list[str] | None = None,
        visit: int = 0,
        baseline: bool = False,
    ) -> None:
        """Plan an empty whiteboard, work it until no task is left, then judge it: a pass
        projects the stage's typed artifacts, a fail adds gap tasks and loops, at most
        MAX_LOOPS times. Raises StageError to fail the stage, Stopped when the run's Budget
        stopped an agent. `baseline` runs one worker on the goal alone, with no graph.

        `notes` are the send-back notes addressed to this stage and `visit` the run's send-back
        count. A stage the run comes back to after send-back `visit` gets one rework task
        carrying the newest note, so its passed board is worked and judged again.

        Every step reads where the stage got to from the whiteboard and its verdicts, so a
        retried stage resumes: a seeded board is not planned again, a worked board is not worked
        again and a passing verdict is projected without asking the judge again."""
        try:
            kind = output_type(config.get("output_type"))
        except ValueError as exc:
            raise StageError(str(exc)) from exc
        at = _At(
            world_id,
            run_id,
            stage_id,
            stage_name,
            config,
            world_folder,
            tuple(notes or ()),
            baseline,
        )
        if baseline:
            # ponytail: no judge, so a baseline stage projects no typed artifacts; later stages
            # see only what the worker wrote. Enough to measure cost and duration.
            goal = str(config.get("goal", "")).strip() or stage_name
            if not self.board.list_tasks(stage_id):
                self.board.add_task(world_id, run_id, stage_id, goal, added_by=WORKER)
            self._work(at)
            return
        tasks = self.board.list_tasks(stage_id)
        if not tasks:
            self._plan(at)
        elif visit and self.board.list_verdicts(stage_id):
            rework = f"Rework after send-back {visit}"
            # ponytail: a stage first run after a send-back and then retried by DBOS gets one
            # needless rework; extra work, never lost work.
            if all(t.title != rework for t in tasks):  # a retried step added it already
                body = at.notes[-1] if at.notes else "An earlier stage was redone. Update the work."
                self.board.add_task(world_id, run_id, stage_id, rework, added_by="gate", body=body)
        while True:
            verdicts = self.board.list_verdicts(stage_id)
            last = verdicts[-1] if verdicts else None
            open_tasks = any(t.status != "done" for t in self.board.list_tasks(stage_id))
            # ponytail: judge loops count across visits, so a revisit gets what the first visit
            # left of MAX_LOOPS; count per visit if reworks start failing on the cap.
            if last and last.passed and not open_tasks:
                self._project(at, kind, last)
                return
            if last and last.loop > MAX_LOOPS:
                raise StageError(
                    f"The judge failed the stage {last.loop} times, the last time because: "
                    + "; ".join(last.reasons)
                )
            self._work(at)
            self._judge(at, kind)

    def _plan(self, at: "_At") -> None:
        goal = str(at.config.get("goal", "")).strip()
        prior = self.files.artifacts(at.world_id, at.run_id)
        prompt = "\n".join(
            [
                f"Seed the whiteboard of swarm stage {at.stage_name}.",
                f"Goal: {goal}",
                f"Done when: {at.config.get('done_when', '')}",
                _wants(at.config),
                f"Artifacts of earlier stages: {', '.join(prior)}." if prior else "",
                self._pins(at),
                "Query the graph for what the world already knows, then answer with the tasks:"
                " small, concrete, each one worker can finish alone. `after` lists the indexes"
                " of earlier tasks in your list that must be done first.",
            ]
        )
        seeds = self._short(at, PLANNER, "planner", prompt, PLAN_SCHEMA, parse_plan)
        self.board.add_tasks(
            at.world_id,
            at.run_id,
            at.stage_id,
            [(t.title, t.body, t.after) for t in seeds],
            added_by=PLANNER,
        )

    def _work(self, at: "_At") -> None:
        """Run the worker while any task is not done. A worker that stops early fails the
        stage: the judge only ever sees a worked-out board."""
        board = self.board
        if all(t.status == "done" for t in board.list_tasks(at.stage_id)):
            return
        stage_tools = [str(t) for t in at.config.get("tools") or []]
        allowed = allowlist(stage_tools, at.baseline)

        def spec(resume: str | None) -> SessionSpec:
            return SessionSpec(
                system_prompt=self._system_prompt(at),
                prompt=f"Work the whiteboard of stage {at.stage_name} until no task is ready.",
                cwd=self.files.stage_folder(at.world_id, at.run_id, at.stage_id),
                plugin=at.world_folder,
                tools=self._tools(
                    at.world_id, at.run_id, at.stage_id, WORKER, self._readable_at(at), at.baseline
                ),
                allowed=allowed,
                servers=self.servers(at.world_id, [t for t in stage_tools if t not in STAGE_TOOLS]),
                resume=resume,
                max_turns=MAX_TURNS,
            )

        self._session(at, WORKER, "worker", spec, allowed, lambda _: None)
        left = [t.title for t in board.list_tasks(at.stage_id) if t.status != "done"]
        if left:
            raise StageError(
                f"{WORKER} stopped with {len(left)} task(s) not done: {'; '.join(left)}"
            )

    def _judge(self, at: "_At", kind: OutputType | None) -> None:
        board, files = self.board, self.files
        loop = len(board.list_verdicts(at.stage_id)) + 1
        notes = files.notes(at.world_id, at.run_id, at.stage_id)
        tasks = board.list_tasks(at.stage_id)
        prior = files.artifacts(at.world_id, at.run_id)
        if kind is None:
            output = "The stage declares no output type: return no items."
        else:
            shape = f"items of type {kind.type}" if kind.many else f"exactly one {kind.type}"
            output = (
                f"On pass, return the stage's output as {shape}, ranked from 1 (best), each"
                " citing the note paragraphs it comes from by note path and paragraph number."
                + (" Put an idea's hook and why in fields." if kind.type == "idea" else "")
            )
        prompt = "\n".join(
            [
                f"Judge swarm stage {at.stage_name}, loop {loop} of at most {MAX_LOOPS + 1}.",
                f"Goal: {at.config.get('goal', '')}",
                f"Done when: {at.config.get('done_when', '')}",
                "Pass only if the done condition is met by what the notes and artifacts show."
                " Give your reasons. On fail, name each missing piece as a gap task title; the"
                " workers take them and you judge again.",
                output,
                f"Artifacts of the run: {', '.join(prior)}." if prior else "",
                *(f"A reviewer sent the stage back with this note: {n}" for n in at.notes),
                "# Whiteboard",
                *[
                    f"- {t.title} ({t.status}): " + " / ".join(n.body for n in t.notes)
                    for t in tasks
                ],
                "# Notes",
                notes_for_judge(notes),
            ]
        )
        j = self._short(at, JUDGE, "judge", prompt, VERDICT_SCHEMA, parse_verdict)
        file = verdict_file(loop, j)
        path = files.write_typed(
            at.world_id,
            at.run_id,
            at.stage_id,
            JUDGE,
            file.name,
            file.type,
            file.fields,
            file.body,
        )
        board.record_verdict(
            at.world_id,
            at.run_id,
            at.stage_id,
            passed=j.passed,
            reasons=j.reasons,
            gaps=j.gaps,
            items=[asdict(i) for i in j.items],
            path=path,
            added_by=JUDGE,
        )

    def _project(self, at: "_At", kind: OutputType | None, verdict: Verdict) -> None:
        if kind is None:
            return
        items = [
            Item(
                i["title"],
                i["body"],
                i.get("rank"),
                i.get("fields") or {},
                [Source(s["note"], s["paragraph"]) for s in i.get("sources") or []],
            )
            for i in verdict.items
        ]
        notes = self.files.notes(at.world_id, at.run_id, at.stage_id)
        for a in project(kind, items, notes):
            self.files.write_typed(
                at.world_id, at.run_id, at.stage_id, JUDGE, a.name, a.type, a.fields, a.body
            )

    def _short(
        self,
        at: "_At",
        name: str,
        role: AgentRole,
        prompt: str,
        schema: dict[str, Any],
        parse: Callable[[Any], T],
    ) -> T:
        """A planner or judge session: the graph and the whiteboard to read, a JSON answer."""
        allowed = [tool_id(t) for t in SHORT_TOOLS] + BUILTIN
        tools = [
            t
            for t in self._tools(at.world_id, at.run_id, at.stage_id, name, self._readable_at(at))
            if t.name in SHORT_TOOLS
        ]
        system = f"You are the {role} of a swarm. Answer only with the JSON the schema asks for."

        def spec(resume: str | None) -> SessionSpec:
            return SessionSpec(
                system_prompt=system,
                prompt=prompt,
                cwd=self.files.stage_folder(at.world_id, at.run_id, at.stage_id),
                plugin=at.world_folder,
                tools=tools,
                allowed=allowed,
                resume=resume,
                max_turns=SHORT_TURNS,
                output_schema=schema,
            )

        return self._session(at, name, role, spec, allowed, parse, fresh=True)

    def _session(
        self,
        at: "_At",
        name: str,
        role: AgentRole,
        spec: Callable[[str | None], SessionSpec],
        allowed: list[str],
        parse: Callable[[Any], T],
        fresh: bool = False,
    ) -> T:
        """Run the agent's session to a usable answer. A session that dies, errors or answers
        what `parse` refuses is replaced once by one resuming it; when both fail the stage
        fails. The first session continues the agent's stored session when it has one, unless
        `fresh`: a planner or judge answer starts clean so verdicts do not carry each other."""
        board = self.board
        before = board.agent(at.world_id, at.run_id, at.stage_id, name, role).session_id
        if reason := self.budget(at.run_id):
            board.log(at.stage_id, name, "session", f"Not started: {reason}")
            raise Stopped(reason)
        board.update_agent(at.stage_id, name, status="working")
        hooks = _Hooks(
            board,
            at.stage_id,
            name,
            allowed,
            at.world_folder,
            lambda: self.budget(at.run_id),
            graph_first=not at.baseline,
        )
        for attempt in range(SESSIONS):
            stored = board.agent(at.world_id, at.run_id, at.stage_id, name, role).session_id
            resume = None if fresh and stored == before else stored
            if resume:
                said = "A replacement resumes" if attempt else "Continuing"
                board.log(at.stage_id, name, "session", f"{said} session {resume}.")
            try:
                end = asyncio.run(self.runtime.run(spec(resume), hooks))
            except Exception as exc:
                board.log(at.stage_id, name, "error", f"The session died: {exc}")
                continue
            if end.stopped:
                detail = f"Stopped: {end.stopped} It resumes once money is added."
                board.log(at.stage_id, name, "session", detail)
                raise Stopped(end.stopped)
            if end.is_error:
                board.log(at.stage_id, name, "error", f"The session failed: {end.result}")
                continue
            try:
                answer = parse(end.output)
            except ValueError as exc:
                board.log(at.stage_id, name, "error", f"The answer was not usable: {exc}")
                continue
            board.log(
                at.stage_id,
                name,
                "result",
                f"Session ended at an estimated ${end.cost_usd:.4f}. {end.result}".strip(),
            )
            board.update_agent(at.stage_id, name, status="done")
            return answer
        board.update_agent(at.stage_id, name, status="failed")
        raise StageError(f"{name} failed {SESSIONS} sessions in a row; see the stage log")

    def _pins(self, at: "_At") -> str:
        """Pinned artifacts are required reading for the planner and the worker (Memory's Pin);
        the judge judges the stage's own notes."""
        pins = self.files.pinned(at.world_id)
        if not pins:
            return ""
        paths = ", ".join(str(at.world_folder / p) for p in pins)
        return f"Pinned references, required reading before you start (Read each): {paths}."

    def _system_prompt(self, at: "_At") -> str:
        prior = self.files.artifacts(at.world_id, at.run_id)
        return "\n".join(
            [
                f"You are {WORKER}, a worker in a swarm. Stage: {at.stage_name}.",
                f"Goal: {at.config.get('goal', '')}",
                f"Done when: {at.config.get('done_when', '')}",
                _wants(at.config),
                f"Artifacts of earlier stages: {', '.join(prior)}." if prior else "",
                self._pins(at),
                *(f"Sent back to you with this note: {n}" for n in at.notes),
                "How you work: "
                + (
                    ""
                    if at.baseline
                    else "call query_graph before your first claim and reuse what the world knows. "
                )
                + "claim_task takes the next ready task. heartbeat at least every minute"
                " while you hold one. Write what you learn with write_note and outputs with"
                " write_artifact. finish_task with a short note. add_task for work you discover."
                " Stop when no task is ready.",
            ]
        )

    def _readable_at(self, at: "_At") -> dict[str, Path]:
        return self._readable(at.world_id, at.world_folder, at.config)

    def _readable(
        self, world_id: str, world_folder: Path, config: dict[str, Any]
    ) -> dict[str, Path]:
        """The graphs the stage may read, by the names the agent may use for them."""
        readable = {world_id: world_folder}
        for ref in [str(g) for g in config.get("graphs") or []]:
            folder = self.graphs.folder(ref)
            if folder is not None:
                readable[ref] = folder
        return readable

    def _graph(self, readable: dict[str, Path], tool: str, args: dict[str, Any]) -> str:
        args = dict(args)
        ref = args.pop("world", None) or args.pop("project_path", None)
        args.pop("project_path", None)
        name = next(iter(readable)) if not ref else str(ref)
        if name not in readable:
            target = Path(name).expanduser().resolve()
            name = next((n for n, f in readable.items() if f.resolve() == target), "")
        if not name:
            raise ToolError(
                f"The graph {ref} is not readable in this stage. Readable: {', '.join(readable)}."
            )
        return f"[world {name}]\n{self.graphs.call(readable[name], tool, args)}"

    def _tools(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        agent: str,
        readable: dict[str, Path],
        baseline: bool = False,
    ) -> list[AgentTool]:
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

        def graph_tool(tool: str) -> Callable[[dict[str, Any]], str]:
            return lambda a: self._graph(readable, tool, a)

        handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            **{tool: graph_tool(tool) for tool in GRAPH_TOOLS},
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
            if not (baseline and name in GRAPH_TOOLS)
        ]


class _Hooks:
    """The SessionHooks of one agent: the live log, the allowlist and the graph-first rule."""

    def __init__(
        self,
        board: Storage,
        stage_id: str,
        agent: str,
        allowed: list[str],
        world_folder: Path,
        budget: Callable[[], str | None] = lambda: None,
        graph_first: bool = True,
    ) -> None:
        self.board = board
        self.stage_id = stage_id
        self.agent = agent
        self.allowed = allowed
        self.world_folder = world_folder.resolve()
        self.budget = budget
        self.graph_first = graph_first
        self.stopped: str | None = None

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

    def cost(self, usd: float) -> str | None:
        """Add the estimate to the agent, then ask the Budget whether the session goes on."""
        self.board.update_agent(self.stage_id, self.agent, cost_usd=usd)
        self.stopped = self.budget()
        return self.stopped

    def _refusal(self, tool: str, input: dict[str, Any]) -> str | None:
        if self.stopped:  # a call already on its way when the Budget ran out
            return f"Stopped: {self.stopped}"
        if not is_allowed(tool, self.allowed):
            return f"{tool} is not allowed in this stage."
        if tool == "Read":
            path = Path(str(input.get("file_path", "")))
            if not path.is_absolute() or not path.resolve().is_relative_to(self.world_folder):
                return "Read is limited to this world's folder."
        if (
            tool == CLAIM
            and self.graph_first
            and not any(
                e.agent == self.agent and e.kind == "tool" and e.tool == GRAPH
                for e in self.board.list_log(self.stage_id)
            )
        ):
            return "Query the graph first: call query_graph about the work before your first claim."
        return None
