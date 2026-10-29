"""Workers working a swarm stage's whiteboard through the AgentRuntime port.

Each worker's session gets the in-process tools below, the stage's allowlist and the world's
skills. The world's Cast (handed in by the composition root as `CastEntry` rows) names the
workers, adds each agent's standing prompt to its system prompt and may narrow its tools; as
many workers start as there are ready tasks, up to the cast's size. Each agent's hooks write
every tool call to the stage's live log, deny tools outside the allowlist and a claim before the
agent's first graph query, and keep the agent's session id and estimated cost. A session that
dies is replaced by one that resumes the stored session id.

After every cost estimate the hooks ask the run's stop check (`budget`, from the composition
root); once the Budget is spent, or the run is paused or aborted, they stop the session and the
stage raises Stopped, leaving the agent's session to resume when the run goes on. A baseline
stage is one worker with no graph: no planner, no judge, no graph tools and no graph-first rule,
the single-agent measure the swarm is compared against.

Memory and the graph are other contexts, so the composition root hands in `Files` and `Graphs`.
Graph tools are wrapped: an agent may name the graph of its own world or of a world the stage
lists under `graphs`, by id, name or folder, and nothing else (ADR 0009). Every answer is labelled
with the world it came from.
"""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from swarmify_swarm import build as builds
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

    def assets(self, world_id: str) -> list[str]:
        """The world's read-only assets (music, sound effects, logos), as paths relative to
        its folder."""
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

    def name(self, folder: Path) -> str:
        """The name of the world at `folder`, which labels every answer from its graph."""
        ...

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str: ...


class NoGraphs:
    def folder(self, world: str) -> Path | None:
        return None

    def name(self, folder: Path) -> str:
        return folder.name

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        return "The world graph has nothing yet. Work from the task and the stage goal."


@dataclass(frozen=True)
class CastEntry:
    """One agent of the world's Cast as the swarm runs it. `tools` narrows the stage's tools to
    these names; None allows whatever the stage allows. `model` is the model its sessions run
    on; None leaves it to the runtime."""

    name: str
    role: AgentRole
    prompt: str = ""
    tools: list[str] | None = None
    model: str | None = None


DEFAULT_CAST = [
    CastEntry(PLANNER, "planner"),
    CastEntry(WORKER, "worker"),
    CastEntry(JUDGE, "judge"),
]


class StageError(RuntimeError):
    """The swarm stage failed; the message lands on the stage."""


class Stopped(Exception):
    """The run's stop check (its Budget, a pause or an abort) stopped the stage's agents. Not a
    failure: the stage runs again, its agents resuming their sessions, once the run goes on."""


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
    cast: tuple[CastEntry, ...] = tuple(DEFAULT_CAST)

    def entry(self, name: str, role: AgentRole) -> CastEntry:
        return next((c for c in self.cast if c.name == name), CastEntry(name, role))

    @property
    def workers(self) -> list[CastEntry]:
        """The cast's workers; a baseline stage runs only the first (one agent, no graph)."""
        workers = [c for c in self.cast if c.role == "worker"] or [CastEntry(WORKER, "worker")]
        return workers[:1] if self.baseline else workers


def _decisions(world_folder: Path, run_id: str) -> list[str]:
    """The newest decision of each gate of the run, as required reading. A gate whose newest
    decision is a send-back is skipped: its note reaches the stage it named, and the gate asks
    again before any later stage runs."""
    newest: dict[str, tuple[str, Path, dict[str, Any], str]] = {}
    for path in (world_folder / "runs" / run_id).glob("*/decision*.md"):
        head, _, body = path.read_text(errors="replace").removeprefix("---\n").partition("\n---\n")
        fields = {k: json.loads(v) for k, _, v in (ln.partition(": ") for ln in head.splitlines())}
        created = str(fields.get("created", ""))
        if path.parent.name not in newest or created > newest[path.parent.name][0]:
            newest[path.parent.name] = (created, path, fields, body)
    out = []
    for _, path, fields, body in sorted(newest.values(), key=lambda d: d[0]):
        if fields.get("send_back_to"):
            continue
        outcome = " ".join(ln for ln in body.splitlines()[1:] if ln.strip())
        artifact = fields.get("artifact")
        picked = f" The artifact it names (Read it): {world_folder / artifact}." if artifact else ""
        out.append(
            f"Decision at gate {fields.get('stage')}, required reading before you start:"
            f" {outcome}{picked} The decision: {path}."
        )
    return out


def revision(note: str, previous: Sequence[str]) -> str:
    """The framing of a stage the run came back to with work already done: revise it, do not
    start over. `note` is the send-back note addressed to the stage, if the send-back named it."""
    asked = (
        f"The operator sent it back with this note, required reading: {note}"
        " Change what the note asks and keep the rest as it is."
        if note
        else "An earlier stage was redone: change what that needs and keep the rest as it is."
    )
    read = ", ".join(previous)
    read = f" The previous version, required reading before you change anything: {read}."
    return f"This is a revision, not a fresh start. {asked}{read if previous else ''}"


def _wants(config: dict[str, Any]) -> str:
    produces = ", ".join(str(p) for p in config.get("produces") or [])
    out = f"Write these artifacts: {produces}." if produces else ""
    if config.get("output_type"):
        out += f" The judge projects the notes into output type {config['output_type']}."
    return out.strip()


def no_servers(_world_id: str, _names: list[str]) -> dict[str, dict[str, Any]]:
    return {}


def allowlist(
    stage_tools: list[str], baseline: bool = False, agent_tools: list[str] | None = None
) -> list[str]:
    """`agent_tools`, an agent's own allowed stage tools from the Cast, narrows `stage_tools`."""
    allowed = [tool_id(t) for t in TOOLS if not (baseline and t in GRAPH_TOOLS)] + BUILTIN
    for name in stage_tools:
        if agent_tools is None or name in agent_tools:
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
        "Record partial progress on a task you hold as a note (your tool calls keep the claim).",
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
    "list_assets": (
        "The world's assets: reusable files the operator added (music beds, sound effects,"
        " logos), read-only, as paths relative to the world folder. Name one in a script or"
        " brief for a later stage to use.",
        _obj(),
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
        cast: list[CastEntry] | None = None,
    ) -> None:
        """Plan an empty whiteboard, work it until no task is left, then judge it: a pass
        projects the stage's typed artifacts, a fail adds gap tasks and loops, at most
        MAX_LOOPS times. Raises StageError to fail the stage, Stopped when the run's Budget
        stopped an agent. `baseline` runs one worker on the goal alone, with no graph. `cast`
        is the world's Cast (a planner, one worker and a judge with no prompts by default).

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
            tuple(cast) if cast else tuple(DEFAULT_CAST),
        )
        if baseline:
            # ponytail: no judge, so a baseline stage projects no typed artifacts; later stages
            # see only what the worker wrote. Enough to measure cost and duration.
            goal = str(config.get("goal", "")).strip() or stage_name
            if not self.board.list_tasks(stage_id):
                self.board.add_task(world_id, run_id, stage_id, goal, added_by=at.workers[0].name)
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
                own = self.files.stage_folder(world_id, run_id, stage_id)
                made = [own.parent / a for a in self.files.artifacts(world_id, run_id)]
                mine = [str(a) for a in made if a.is_relative_to(own)]
                body = revision(at.notes[-1] if at.notes else "", mine)
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
        """Run the workers while any task is not done: one per ready task, up to the cast's
        workers, each in its own thread over the shared whiteboard. Workers that stop early fail
        the stage: the judge only ever sees a worked-out board."""
        board = self.board
        tasks = board.list_tasks(at.stage_id)
        if all(t.status == "done" for t in tasks):
            return
        done = {t.id for t in tasks if t.status == "done"}
        ready = [t for t in tasks if t.status == "open" and all(d in done for d in t.depends_on)]
        workers = at.workers[: max(1, len(ready))]
        failures: list[Exception] = []

        def work(entry: CastEntry) -> None:
            try:
                self._worker(at, entry)
            except (Stopped, StageError) as exc:
                failures.append(exc)

        if len(workers) == 1:
            work(workers[0])
        else:
            with ThreadPoolExecutor(len(workers), thread_name_prefix="worker") as pool:
                list(pool.map(work, workers))
        # The Budget stopping an agent is not a failure: the stage waits for money (Stopped).
        for exc in [*(e for e in failures if isinstance(e, Stopped)), *failures]:
            raise exc
        left = [t.title for t in board.list_tasks(at.stage_id) if t.status != "done"]
        if left:
            names = ", ".join(w.name for w in workers)
            raise StageError(
                f"{names} stopped with {len(left)} task(s) not done: {'; '.join(left)}"
            )

    def _worker(self, at: "_At", entry: CastEntry) -> None:
        stage_tools = [str(t) for t in at.config.get("tools") or []]
        allowed = allowlist(stage_tools, at.baseline, entry.tools)
        servers = [t for t in stage_tools if t not in STAGE_TOOLS]
        if entry.tools is not None:
            servers = [t for t in servers if t in entry.tools]

        def spec(resume: str | None) -> SessionSpec:
            return SessionSpec(
                system_prompt=self._system_prompt(at, entry),
                prompt=f"Work the whiteboard of stage {at.stage_name} until no task is ready.",
                cwd=self.files.stage_folder(at.world_id, at.run_id, at.stage_id),
                plugin=at.world_folder,
                tools=self._tools(
                    at.world_id,
                    at.run_id,
                    at.stage_id,
                    entry.name,
                    self._readable_at(at),
                    at.baseline,
                ),
                allowed=allowed,
                servers=self.servers(at.world_id, servers),
                resume=resume,
                max_turns=MAX_TURNS,
                model=entry.model,
            )

        self._session(at, entry.name, "worker", spec, allowed, lambda _: None)

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
                " with a short reason for its rank, citing the note paragraphs it comes from"
                " by note path and paragraph number."
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
                i.get("reason") or "",
            )
            for i in verdict.items
        ]
        notes = self.files.notes(at.world_id, at.run_id, at.stage_id)
        for a in project(kind, items, notes):
            self.files.write_typed(
                at.world_id, at.run_id, at.stage_id, JUDGE, a.name, a.type, a.fields, a.body
            )

    def ask(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        stage_name: str,
        world_folder: Path,
        name: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        parse: Callable[[Any], T],
        model: str | None = None,
    ) -> T:
        """One judge-style session outside a swarm stage, for another context's question (the
        Learning retro): the graph and the world folder to read, the world's skills loaded, a
        JSON answer that `parse` accepts. Logged, costed and budgeted like any agent of the
        stage, under `name`. Raises StageError when two sessions fail, Stopped when the run's
        Budget stopped it."""
        cast = (CastEntry(name, "judge", model=model),)
        at = _At(world_id, run_id, stage_id, stage_name, {}, world_folder, cast=cast)
        return self._short(at, name, "judge", prompt, schema, parse, system)

    def build(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        stage_name: str,
        world_folder: Path,
        folder: Path,
        script: Path,
        *,
        composition: str,
        brief: str,
        sandbox: dict[str, Any],
        env: dict[str, str],
        data_dir: Path,
        model: str | None = None,
        max_turns: int = builds.MAX_TURNS,
        minutes: float = builds.MINUTES,
        inputs: Sequence[Path] = (),
        revises: Sequence[Path] = (),
        note: str = "",
    ) -> None:
        """One sandboxed coding session writing the Remotion project in `folder` (see build.py),
        with the run's gate decisions as required reading. `revises` names the previous version
        when the session revises it after a send-back with `note`, instead of starting over.
        Logged, costed and budgeted like any agent, as `builder`, whose closing summary ends the
        stage log. Raises StageError, with the builder's last log lines, when two sessions fail
        or time runs out; Stopped when the run's Budget stopped it."""
        cast = (CastEntry(builds.AGENT, builds.ROLE, model=model),)
        at = _At(world_id, run_id, stage_id, stage_name, {}, world_folder, cast=cast)
        decisions = _decisions(world_folder, run_id)
        assets = [world_folder / a for a in self.files.assets(world_id)]
        hooks = _BuildHooks(
            self.board, stage_id, builds.AGENT, builds.TOOLS, world_folder,
            lambda: self.budget(run_id), graph_first=False, clock=self.clock,
        )  # fmt: skip
        hooks.folder, hooks.data_dir = folder, data_dir

        def spec(resume: str | None) -> SessionSpec:
            return SessionSpec(
                system_prompt=builds.SYSTEM,
                prompt="\n".join(
                    ([revision(note, [str(p) for p in revises])] if revises else [])
                    + [builds.prompt(script, composition, brief, decisions, inputs, assets)]
                ),
                cwd=folder,
                # No skills plugin: Claude Code refuses Write and Edit inside a plugin folder,
                # and the build folder lies inside the world folder.
                plugin=None,
                tools=[],
                allowed=builds.TOOLS,
                resume=resume,
                max_turns=max_turns,
                model=model,
                sandbox=sandbox,
                env=env,
            )

        try:
            self._session(
                at, builds.AGENT, builds.ROLE, spec, builds.TOOLS, lambda _: None,
                fresh=True, hooks=hooks, timeout=minutes * 60,
            )  # fmt: skip
        except StageError as exc:
            raise StageError(f"{exc}\n{builds.last_lines(self.board, stage_id)}") from exc

    def _short(
        self,
        at: "_At",
        name: str,
        role: AgentRole,
        prompt: str,
        schema: dict[str, Any],
        parse: Callable[[Any], T],
        system: str | None = None,
    ) -> T:
        """A planner or judge session: the graph and the whiteboard to read, a JSON answer."""
        allowed = [tool_id(t) for t in SHORT_TOOLS] + BUILTIN
        tools = [
            t
            for t in self._tools(at.world_id, at.run_id, at.stage_id, name, self._readable_at(at))
            if t.name in SHORT_TOOLS
        ]
        if system is None:
            system = (
                f"You are the {role} of a swarm. Answer only with the JSON the schema asks for."
            )
        entry = at.entry(name, role)
        if entry.prompt:
            system += f"\nYour standing instructions: {entry.prompt}"

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
                model=entry.model,
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
        hooks: "_Hooks | None" = None,
        timeout: float | None = None,
    ) -> T:
        """Run the agent's session to a usable answer. A session that dies, errors or answers
        what `parse` refuses is replaced once by one resuming it; when both fail the stage
        fails. The first session continues the agent's stored session when it has one, unless
        `fresh`: a planner or judge answer starts clean so verdicts do not carry each other.
        `timeout` (seconds) bounds all its sessions together; running out fails the stage."""
        board = self.board
        before = board.agent(at.world_id, at.run_id, at.stage_id, name, role).session_id
        if reason := self.budget(at.run_id):
            board.log(at.stage_id, name, "session", f"Not started: {reason}")
            raise Stopped(reason)
        board.update_agent(at.stage_id, name, status="working")
        hooks = hooks or _Hooks(
            board,
            at.stage_id,
            name,
            allowed,
            at.world_folder,
            lambda: self.budget(at.run_id),
            graph_first=not at.baseline,
            clock=self.clock,
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        for attempt in range(SESSIONS):
            stored = board.agent(at.world_id, at.run_id, at.stage_id, name, role).session_id
            resume = None if fresh and stored == before else stored
            if resume:
                said = "A replacement resumes" if attempt else "Continuing"
                board.log(at.stage_id, name, "session", f"{said} session {resume}.")
            left = None if deadline is None else max(deadline - time.monotonic(), 0)
            try:
                end = asyncio.run(asyncio.wait_for(self.runtime.run(spec(resume), hooks), left))
            except TimeoutError:
                assert timeout is not None
                said = f"Out of time: the limit is {timeout / 60:g} minutes."
                board.log(at.stage_id, name, "error", said)
                board.update_agent(at.stage_id, name, status="failed")
                raise StageError(f"{name} ran out of time ({timeout / 60:g} minutes)") from None
            except Exception as exc:
                board.log(at.stage_id, name, "error", f"The session died: {exc}")
                continue
            if end.stopped:
                board.log(at.stage_id, name, "session", f"Stopped: {end.stopped}")
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
        """Pinned artifacts and the run's gate decisions are required reading for the planner
        and the worker (Memory's Pin); the judge judges the stage's own notes."""
        pins = self.files.pinned(at.world_id)
        paths = ", ".join(str(at.world_folder / p) for p in pins)
        read = f"Pinned references, required reading before you start (Read each): {paths}."
        return "\n".join(([read] if pins else []) + _decisions(at.world_folder, at.run_id))

    def _system_prompt(self, at: "_At", entry: CastEntry) -> str:
        prior = self.files.artifacts(at.world_id, at.run_id)
        return "\n".join(
            [
                f"You are {entry.name}, a worker in a swarm. Stage: {at.stage_name}.",
                f"Your standing instructions: {entry.prompt}" if entry.prompt else "",
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
                + "claim_task takes the next ready task; your claim stays yours while you work."
                " heartbeat with a note to record partial progress. Write what you learn with"
                " write_note and outputs with write_artifact. finish_task with a short note."
                " add_task for work you discover. Stop when no task is ready.",
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
        folder = readable[name]
        return f"[world {self.graphs.name(folder)}]\n{self.graphs.call(folder, tool, args)}"

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
            "list_assets": lambda a: json.dumps(files.assets(world_id)),
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
    """The SessionHooks of one agent: the live log, the allowlist, the graph-first rule and the
    renewal of its claims on every tool call and model step, so a working agent keeps them
    without heartbeating and a dead session's claims still expire."""

    def __init__(
        self,
        board: Storage,
        stage_id: str,
        agent: str,
        allowed: list[str],
        world_folder: Path,
        budget: Callable[[], str | None] = lambda: None,
        graph_first: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.board = board
        self.clock = clock
        self.stage_id = stage_id
        self.agent = agent
        self.allowed = allowed
        self.world_folder = world_folder.resolve()
        self.budget = budget
        self.graph_first = graph_first
        self.stopped: str | None = None

    def started(self, init: SessionInit) -> None:
        self.board.update_agent(
            self.stage_id,
            self.agent,
            session_id=init.session_id,
            skills=init.skills,
            model=init.model or None,
        )
        detail = (
            f"Session {init.session_id} started on {init.model or 'the default model'}"
            f" with skills: {', '.join(init.skills) or 'none'}."
        )
        if init.plugin_errors:
            detail += f" The skills plugin reported: {'; '.join(init.plugin_errors)}."
        self.board.log(
            self.stage_id, self.agent, "error" if init.plugin_errors else "session", detail
        )

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        self._renew()
        reason = self._refusal(tool, input)
        kind = "denied" if reason else "tool"
        self.board.log(self.stage_id, self.agent, kind, reason or "", tool=tool, input=input)
        return reason

    def cost(self, usd: float) -> str | None:
        """Add the estimate to the agent, then ask the Budget whether the session goes on."""
        self.board.update_agent(self.stage_id, self.agent, cost_usd=usd)
        self._renew()
        self.stopped = self.budget()
        return self.stopped

    def _renew(self) -> None:
        for task in self.board.list_tasks(self.stage_id):
            if task.status == "claimed" and task.owner == self.agent:
                try:
                    self.board.heartbeat(task.id, self.agent, now=self.clock())
                except Refused:  # expired or finished since the listing
                    pass

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


class _BuildHooks(_Hooks):
    """A build session's hooks: the live log and the allowlist as for any agent, then the build
    folder's path rules (build.py) instead of the world-folder Read rule."""

    folder: Path
    data_dir: Path

    def _refusal(self, tool: str, input: dict[str, Any]) -> str | None:
        if self.stopped:
            return f"Stopped: {self.stopped}"
        if not is_allowed(tool, self.allowed):
            return f"{tool} is not allowed in this stage."
        return builds.refusal(tool, input, self.folder, self.world_folder, self.data_dir)
