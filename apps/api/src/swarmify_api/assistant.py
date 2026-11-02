"""The Run Assistant (spec Run Assistant, ADR 0027): a chat on a run's page that reads the run and
proposes Run Changes. An agent session on the AgentRuntime, like the Planner, on the world's
planner model, one conversation per run, resumed by session id.

Its tools read the run (its stages, config, spend and revision; a file of its folder; a stage's
agent log; its events and changes; its workflow's versions and recent runs; the world's graph),
and one writes nothing: `propose_change` calls `RunControl.propose` and keeps the proposal for
the conversation, as a card the operator applies through the Run Change seam or dismisses. The
deny hook refuses every other tool, so the assistant can never apply a change or answer a gate.
Each turn's cost counts against the world's monthly spend (`RunsWithAssistant`), never the run's
Budget."""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from swarmify_kernel import new_id
from swarmify_kernel.sqlite import connect
from swarmify_memory import GraphIndex
from swarmify_runs import ChangeProposal, Illegal, PublishPreview, Run, RunChange, Runner
from swarmify_runs import SqliteStorage as RunStorage
from swarmify_swarm import (
    AgentRuntime,
    AgentTool,
    SessionEnd,
    SessionInit,
    SessionSpec,
    Storage,
    ToolError,
    tool_id,
)
from swarmify_workflows import Workflows
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)
MAX_TURNS = 30
MAX_READ = 40_000
"""The most characters of a run file one read returns."""
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "What you tell the operator, in plain words"},
        "sources": {
            "type": "array",
            "description": "Where the answer comes from, each shown as a link to its stage",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": 'Like "from script.md"'},
                    "stage": {"type": "string", "description": "The stage's id"},
                },
                "required": ["label", "stage"],
            },
        },
        "proposals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The ids of the proposals this reply presents, from propose_change",
        },
        "fields": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "At a publish gate: the gate's editable fields to fill, by key",
        },
    },
    "required": ["reply"],
}
SYSTEM = """You are the Run Assistant of swarmify, on the page of one run. The operator asks about
the run and asks for changes to it. Answer only with the JSON the schema asks for.

Read the run before you answer: get_run (its stages, their status and config, spend, revision
and files), read_run_file, tail_stage_log, list_run_events (its events and Run Changes),
list_workflow_runs (its workflow's versions and recent runs) and query_graph (the world's
memory of earlier runs). Ground every answer in what you read and list each place in `sources`
with the stage's id and a short label such as "from script.md" or "Research, log 14:02". When
you cannot see something, say so plainly; never guess.

To change the run, call propose_change with a Run Change: `kind` stage_edit, `stage` the id of a
stage that has not started, `payload` its whole new config. It writes nothing: it answers what
the change would do and what the check refuses. Fix a refused proposal and propose again, then
list the ids of the proposals you present in `proposals`. Nothing changes until the operator
presses Apply on a card. You cannot apply a change or answer a gate, and never say you did."""
GATE = """The publish stage after the gate sends this, as the gate's form shows it, with what would
stop the post: {preview}

To change an editable field of the form (title, description, hashtags, tags), answer it in
`fields` by key with its whole new value: it fills the gate's form for the operator to confirm,
so propose no change for it. To change a file the post reads (like metadata.md), call
propose_change with `kind` artifact_edit, `stage` the id of the finished stage that made it and
`payload` {{"path": its name, "text": its whole new text}}."""


class Ask(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class Source(BaseModel):
    label: str
    stage: str
    """The id of the stage the answer read, which the link opens."""


class Card(BaseModel):
    """A Run Change the assistant proposed, as `RunControl.propose` returned it. `stale` once
    the run moved past the proposal's revision while it was open."""

    id: str
    state: Literal["open", "applied", "dismissed", "stale"]
    proposal: ChangeProposal
    change_id: str | None = None
    """The applied change, once applied."""


class Message(BaseModel):
    id: str
    role: Literal["operator", "assistant"]
    text: str
    at: str
    sources: list[Source] = []
    proposals: list[Card] = []
    cost_usd: float = 0.0
    fields: dict[str, str] = {}
    """At a publish gate, the gate's editable fields this reply fills in its form, by key: an
    answer of the turn only, never kept, as the gate's form is the operator's to confirm."""


class Conversation(BaseModel):
    messages: list[Message]
    spent_usd: float
    """What asking this run has cost, from the world's monthly budget."""


class Mark(BaseModel):
    state: Literal["applied", "dismissed"]
    change_id: str | None = None
    """For `applied`: the change the Run Change seam recorded."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Conversations:
    """The conversations, in the API's database beside the run they belong to."""

    def __init__(self, db: Path) -> None:
        self.db = db

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.db))

    def propose(self, run_id: str, proposal: ChangeProposal) -> str:
        """Keep a proposal made during a turn, not yet part of a message."""
        made = new_id("proposal")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO assistant_proposals (id, run_id, proposal) VALUES (?, ?, ?)",
                (made, run_id, json.dumps(jsonable_encoder(proposal))),
            )
        return made

    def drop(self, ids: list[str]) -> None:
        with self._conn() as conn:
            conn.executemany("DELETE FROM assistant_proposals WHERE id = ?", [(i,) for i in ids])

    def turn(
        self, run: Run, question: str, reply: str, sources: list[Source], shown: list[str],
        made: list[str], session_id: str, cost_usd: float,
    ) -> str:  # fmt: skip
        """Record one exchange: the operator's message, the reply with the proposals it shows,
        in order; the turn's other proposals go. Returns the reply's id."""
        asked, answered = new_id("message"), new_id("message")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            said = json.dumps([s.model_dump() for s in sources])
            conn.executemany(
                "INSERT INTO assistant_messages (id, run_id, world_id, role, text, sources,"
                " session_id, cost_usd, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (asked, run.id, run.world_id, "operator", question, "[]", None, 0, _now()),
                    (answered, run.id, run.world_id, "assistant", reply, said, session_id,
                     cost_usd, _now()),
                ],
            )  # fmt: skip
            conn.executemany(
                "UPDATE assistant_proposals SET message_id = ?, position = ? WHERE id = ?",
                [(answered, n, p) for n, p in enumerate(shown)],
            )
            conn.executemany(
                "DELETE FROM assistant_proposals WHERE id = ?",
                [(p,) for p in made if p not in shown],
            )
            conn.execute("COMMIT")
        return answered

    def failed(self, run: Run, error: str, cost_usd: float) -> None:
        """Keep what a failed turn cost, out of the conversation: the session still billed it."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO assistant_messages (id, run_id, world_id, role, text, cost_usd, at)"
                " VALUES (?, ?, ?, 'failed', ?, ?, ?)",
                (new_id("message"), run.id, run.world_id, error, cost_usd, _now()),
            )

    def session(self, run_id: str) -> str | None:
        """The session the conversation resumes, None before its first answer."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id FROM assistant_messages WHERE run_id = ? AND role = 'assistant'"
                " ORDER BY at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return row[0] if row else None

    def messages(self, run: Run) -> list[Message]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assistant_messages WHERE run_id = ? AND role != 'failed'"
                " ORDER BY at, rowid",
                (run.id,),
            ).fetchall()
            cards = conn.execute(
                "SELECT * FROM assistant_proposals WHERE run_id = ? AND message_id IS NOT NULL"
                " ORDER BY position",
                (run.id,),
            ).fetchall()
        return [
            Message(
                id=r["id"],
                role=r["role"],
                text=r["text"],
                at=r["at"],
                sources=json.loads(r["sources"]),
                proposals=[_card(c, run) for c in cards if c["message_id"] == r["id"]],
                cost_usd=r["cost_usd"],
            )
            for r in rows
        ]

    def card(self, run: Run, proposal_id: str) -> Card | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_proposals WHERE id = ? AND run_id = ?",
                (proposal_id, run.id),
            ).fetchone()
        return _card(row, run) if row else None

    def mark(self, run: Run, proposal_id: str, state: str, change_id: str | None) -> Card | None:
        """Close an open proposal; None when it was already applied or dismissed."""
        with self._conn() as conn:
            marked = conn.execute(
                "UPDATE assistant_proposals SET state = ?, change_id = ?"
                " WHERE id = ? AND run_id = ? AND state = 'open'",
                (state, change_id, proposal_id, run.id),
            ).rowcount
        return self.card(run, proposal_id) if marked else None

    def spent(self, run_id: str) -> float:
        with self._conn() as conn:
            (total,) = conn.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM assistant_messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return float(total)

    def spent_in_month(self, world_id: str, month: str) -> float:
        with self._conn() as conn:
            (total,) = conn.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM assistant_messages WHERE world_id = ?"
                " AND at LIKE ?",
                (world_id, f"{month}-%"),
            ).fetchone()
        return float(total)


def _card(row: sqlite3.Row, run: Run) -> Card:
    proposal = json.loads(row["proposal"])
    state = row["state"]
    if state == "open" and proposal["revision"] != run.revision:
        state = "stale"
    return Card.model_validate(
        {"id": row["id"], "state": state, "proposal": proposal, "change_id": row["change_id"]}
    )


class RunsWithAssistant(RunStorage):
    """The runs' storage, whose monthly spend counts what asking runs cost as well: the
    assistant spends the world's Monthly Budget, never a run's Budget."""

    def spent_in_month(self, world_id: str, month: str) -> float:
        asking = Conversations(self.path).spent_in_month(world_id, month)
        return super().spent_in_month(world_id, month) + asking


class _Hooks:
    """Allow only the assistant's tools; remember the session."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed
        self.session_id: str | None = None

    def started(self, init: SessionInit) -> None:
        self.session_id = init.session_id

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        return None if tool in self.allowed else f"{tool} is not allowed in the Run Assistant."

    def cost(self, usd: float) -> str | None:
        return None


async def run_turn(
    runtime: AgentRuntime, worlds: Worlds, conversations: Conversations, run: Run, system: str,
    prompt: str, tools: list[AgentTool], schema: dict[str, Any],
) -> tuple[SessionEnd, str]:  # fmt: skip
    """One turn of the run's conversation on the world's planner model, resumed from its last
    session and allowed only `tools`: how it ended and its session id."""
    allowed = [tool_id(t.name) for t in tools]
    folder = worlds.folder(run.world_id)
    folder.mkdir(parents=True, exist_ok=True)
    world = worlds.get(run.world_id)
    spec = SessionSpec(
        system_prompt=system,
        prompt=prompt,
        cwd=folder,
        plugin=None,
        tools=tools,
        allowed=allowed,
        resume=conversations.session(run.id),
        max_turns=MAX_TURNS,
        output_schema=schema,
        model=world.model_for("planner") if world else None,
    )
    hooks = _Hooks(allowed)
    end = await runtime.run(spec, hooks)
    return end, hooks.session_id or end.session_id


def _schema(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


TEXT = {"type": "string"}
NUMBER = {"type": "integer"}


class Reader:
    """The assistant's tools for one run: every one reads, but `propose_change`, which keeps
    what `RunControl.propose` answers and writes nothing to the run."""

    def __init__(
        self, run_id: str, runner: Runner, worlds: Worlds, workflows: Workflows, board: Storage,
        index: GraphIndex, conversations: Conversations,
    ) -> None:  # fmt: skip
        self.run_id, self.runner, self.worlds, self.workflows = run_id, runner, worlds, workflows
        self.board, self.index, self.conversations = board, index, conversations
        self.made: list[str] = []
        """The proposals made this turn."""

    def run(self) -> Run:
        run = self.runner.get(self.run_id)
        if run is None:
            raise ToolError("the run no longer exists")
        return run

    def folder(self, run: Run) -> Path:
        return self.worlds.folder(run.world_id) / "runs" / run.id

    def tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                "get_run",
                "The run: its stages with their status and config, decisions, Run Changes,"
                " spend, Budget, revision, and the files in its folder.",
                _schema(),
                self.get_run,
            ),
            AgentTool(
                "read_run_file",
                "A text file of the run's folder, by its path as get_run lists it.",
                _schema(path=TEXT),
                self.read_run_file,
            ),
            AgentTool(
                "tail_stage_log",
                "The last lines of a stage's agent log: sessions and the tool calls its agents"
                " made.",
                {**_schema(stage=TEXT), "properties": {"stage": TEXT, "lines": NUMBER}},
                self.tail_stage_log,
            ),
            AgentTool(
                "list_run_events",
                "The run's latest events and every Run Change applied to it.",
                _schema(),
                self.list_run_events,
            ),
            AgentTool(
                "list_workflow_runs",
                "The run's workflow: each version's YAML, and its recent runs with their status"
                " and spend.",
                _schema(),
                self.list_workflow_runs,
            ),
            AgentTool(
                "query_graph",
                "Ask the world's graph, its memory of every run, a question in plain words.",
                _schema(question=TEXT),
                self.query_graph,
            ),
            AgentTool(
                "propose_change",
                "Propose a Run Change. Writes nothing: answers its id, the stage's config before"
                " and after, the violations that would refuse it and the run's revision.",
                _schema(kind=TEXT, stage=TEXT, payload={"type": "object"}),
                self.propose_change,
            ),
        ]

    async def get_run(self, _args: dict[str, Any]) -> str:
        run = self.run()
        folder = self.folder(run)
        files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
        # ponytail: every file listed; page them if a run ever leaves thousands
        seen = {**jsonable_encoder(run), "files": files}
        seen["assistant_usd"] = self.conversations.spent(run.id)
        return json.dumps(seen)

    async def read_run_file(self, args: dict[str, Any]) -> str:
        folder = self.folder(self.run()).resolve()
        path = (folder / str(args.get("path", ""))).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ToolError(f"{args.get('path')} is not a file of this run")
        try:
            return path.read_text()[:MAX_READ]
        except UnicodeDecodeError as exc:
            raise ToolError(f"{args['path']} is not text: ask about it, not its bytes") from exc

    async def tail_stage_log(self, args: dict[str, Any]) -> str:
        stage = str(args.get("stage", ""))
        if not any(s.id == stage for s in self.run().stages):
            raise ToolError(f"no stage {stage} in this run")
        try:
            n = max(1, min(int(args.get("lines") or 50), 500))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"lines must be a number, not {args.get('lines')!r}") from exc
        lines = self.board.list_log(stage)[-n:]
        return json.dumps(jsonable_encoder(lines))

    async def list_run_events(self, _args: dict[str, Any]) -> str:
        run = self.run()
        # ponytail: reads the world's whole stream to find this run's; a run_id filter in the
        # store when worlds grow long
        events = [e for e in self.worlds.events_after(run.world_id) if e.run_id == run.id]
        latest = [
            {"type": e.type, "ts": e.ts, "payload": json.dumps(e.payload)[:300]} for e in events
        ][-50:]
        return json.dumps({"events": latest, "changes": jsonable_encoder(run.changes)})

    async def list_workflow_runs(self, _args: dict[str, Any]) -> str:
        run = self.run()
        if run.workflow_id is None:
            return "This is an Ad Hoc Run: it has no workflow."
        versions = [
            {"number": v.number, "created_at": v.created_at, "yaml": v.yaml}
            for v in self.workflows.versions(run.workflow_id)
        ]
        runs = [
            {"id": r.id, "version": r.workflow_version, "status": r.status,
             "spent_usd": r.spent_usd, "created_at": r.created_at}
            for r in self.runner.list_all(run.world_id)
            if r.workflow_id == run.workflow_id
        ][:10]  # fmt: skip
        return json.dumps({"versions": versions, "runs": runs})

    async def query_graph(self, args: dict[str, Any]) -> str:
        folder = self.worlds.folder(self.run().world_id)
        question = {"question": str(args.get("question", ""))}
        return await to_thread.run_sync(lambda: self.index.call(folder, "query_graph", question))

    async def propose_change(self, args: dict[str, Any]) -> str:
        change = RunChange(str(args.get("kind")), str(args.get("stage")), args.get("payload") or {})
        try:
            proposal = self.runner.propose(self.run_id, change)
        except (KeyError, Illegal, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        made = self.conversations.propose(self.run_id, proposal)
        self.made.append(made)
        return json.dumps({"id": made, **jsonable_encoder(proposal)})


def at_gate(runner: Runner, run: Run) -> tuple[str, PublishPreview | None]:
    """What the session hears of the gate the run waits at: its ask, and what the publish stage
    after it sends, with the fields the gate's form edits, which a reply fills instead of
    proposing a change. Nothing when no gate waits."""
    gate = next((s for s in run.stages if s.type == "gate" and s.status == "waiting"), None)
    if gate is None:
        return "", None
    told = f"The run waits at the gate {gate.name} ({gate.id}), which asks: {gate.ask}"
    preview = runner.preview_after(run, gate.index)
    if preview is None:
        return told, None
    fields = [{"key": f.key, "value": f.value, "editable": f.editable} for f in preview.fields]
    shown = json.dumps({"fields": fields, "problems": preview.problems})
    return "\n\n".join([told, GATE.format(preview=shown)]), preview


def parse_answer(
    output: Any, run: Run, preview: PublishPreview | None = None
) -> tuple[str, list[Source], list[str] | None, dict[str, str]]:
    """The reply, its sources (those naming a stage of the run), the proposal ids it shows,
    None when it names none, and the fields it fills (only those the gate's form edits)."""
    if not isinstance(output, dict) or not str(output.get("reply") or "").strip():
        raise ValueError("the assistant answered without a reply")
    stages = {s.id for s in run.stages}
    sources = [
        Source(label=str(s["label"]), stage=str(s["stage"]))
        for s in output.get("sources") or []
        if isinstance(s, dict) and s.get("label") and s.get("stage") in stages
    ]
    shown = output.get("proposals")
    editable = {f.key for f in preview.fields if f.editable} if preview else set()
    filled = output.get("fields")
    filled = filled if isinstance(filled, dict) else {}
    fields = {k: str(v) for k, v in filled.items() if k in editable}
    reply = str(output["reply"]).strip()
    return reply, sources, [str(p) for p in shown] if shown else None, fields


def router(
    runner: Runner,
    worlds: Worlds,
    workflows: Workflows,
    board: Storage,
    index: GraphIndex,
    runtime: AgentRuntime,
    conversations: Conversations,
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["assistant"])

    def get_run(run_id: str) -> Run:
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    @api.post("/runs/{run_id}/assistant", responses={404: {}, 502: {}})
    async def ask(run_id: str, body: Ask) -> Message:
        """One turn of the Run Assistant: the operator's message in, the reply with its sources
        and proposals out. 502 when the session fails or does not answer in the shape asked,
        with nothing kept but what the session cost; send the message again to retry."""
        run = get_run(run_id)
        reader = Reader(run_id, runner, worlds, workflows, board, index, conversations)
        gate, preview = at_gate(runner, run)
        named = f"The run is {run.name}, id {run.id}, in the world {run.world_id}."
        system = "\n\n".join(filter(None, [SYSTEM, named, gate]))
        end: SessionEnd | None = None
        try:
            end, sid = await run_turn(
                runtime, worlds, conversations, run, system, body.message.strip(),
                reader.tools(), ANSWER_SCHEMA,
            )  # fmt: skip
            if end.is_error:
                raise RuntimeError(end.result or "the session failed")
            reply, sources, shown, fields = parse_answer(end.output, run, preview)
        except Exception as exc:
            conversations.drop(reader.made)
            if end is not None:
                conversations.failed(run, str(exc), end.cost_usd)
            log.warning("run assistant failed on run %s: %s", run_id, exc)
            raise HTTPException(502, f"The assistant did not answer: {exc}") from exc
        shown = [p for p in shown if p in reader.made] if shown is not None else reader.made
        conversations.turn(
            run, body.message.strip(), reply, sources, shown, reader.made, sid, end.cost_usd
        )
        answered = conversations.messages(get_run(run_id))[-1]
        return answered.model_copy(update={"fields": fields})

    @api.get("/runs/{run_id}/assistant", responses={404: {}})
    def conversation(run_id: str) -> Conversation:
        """The run's conversation, oldest first, and what asking it has cost."""
        run = get_run(run_id)
        return Conversation(
            messages=conversations.messages(run), spent_usd=conversations.spent(run_id)
        )

    @api.post(
        "/runs/{run_id}/assistant/proposals/{proposal_id}", responses={404: {}, 409: {}, 422: {}}
    )
    def mark(run_id: str, proposal_id: str, body: Mark) -> Card:
        """Record what became of an open proposal: applied through the Run Change seam (naming
        the change it recorded, 422 unless the run has that change for the proposal's stage and
        kind) or dismissed with Not now. 409 once it was already applied or dismissed."""
        run = get_run(run_id)
        card = conversations.card(run, proposal_id)
        if card is None:
            raise HTTPException(404, "proposal not found")
        if body.state == "applied":
            proposed = card.proposal.change
            if not any(
                c.id == body.change_id and (c.stage_id, c.kind) == (proposed.stage, proposed.kind)
                for c in run.changes
            ):
                raise HTTPException(422, "no such change was applied to this run")
        marked = conversations.mark(run, proposal_id, body.state, body.change_id)
        if marked is None:
            raise HTTPException(409, "the proposal was already applied or dismissed")
        return marked

    return api
