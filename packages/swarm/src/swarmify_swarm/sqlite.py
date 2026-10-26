"""SQLite adapter of the Swarm Storage port. One short connection per call; never migrates.

Every write is one `BEGIN IMMEDIATE` transaction: it takes SQLite's single write lock up front,
so a claim's check and update cannot interleave with another connection's (ADR 0017).
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmify_kernel import Event, new_id
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_swarm.model import (
    HEARTBEAT_TIMEOUT,
    Agent,
    AgentRole,
    AgentStatus,
    LogEntry,
    LogKind,
    Note,
    Refused,
    Task,
    Verdict,
)

Seed = tuple[str, str, tuple[int, ...]]
"""A task to add with others: title, body, indexes of earlier tasks of the batch it needs."""

MIGRATIONS = Path(__file__).parent / "migrations"
SYSTEM = "swarmify"
"""Author of the notes the whiteboard itself writes."""


def _iso(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

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
        with self._write() as conn:
            task_id = _insert_task(
                conn, world_id, run_id, stage_id, title, added_by, body, depends_on
            )
            return self._changed(conn, task_id)

    def add_tasks(
        self, world_id: str, run_id: str, stage_id: str, tasks: list[Seed], *, added_by: str
    ) -> list[Task]:
        with self._write() as conn:
            ids: list[str] = []
            for title, body, after in tasks:
                deps = tuple(ids[i] for i in after)
                ids.append(
                    _insert_task(conn, world_id, run_id, stage_id, title, added_by, body, deps)
                )
            return [self._changed(conn, task_id) for task_id in ids]

    def get_task(self, task_id: str) -> Task | None:
        with closing(connect(self.path)) as conn:
            return _read(conn, task_id)

    def list_tasks(self, stage_id: str) -> list[Task]:
        with closing(connect(self.path)) as conn:
            ids = conn.execute(
                "SELECT id FROM tasks WHERE stage_id = ? ORDER BY created_at, id", (stage_id,)
            ).fetchall()
            return [t for (task_id,) in ids if (t := _read(conn, task_id))]

    def claim(self, task_id: str, agent: str, now: datetime | None = None) -> Task:
        ts = _iso(now)
        with self._write() as conn:
            won = conn.execute(
                "UPDATE tasks SET status = 'claimed', owner = ?, heartbeat_at = ?, updated_at = ?"
                " WHERE id = ? AND owner IS NULL AND NOT EXISTS (SELECT 1 FROM task_deps d"
                "   JOIN tasks b ON b.id = d.depends_on"
                "   WHERE d.task_id = ? AND b.status != 'done')",
                (agent, ts, ts, task_id, task_id),
            ).rowcount
            if not won:
                task = _read(conn, task_id)
                if task is None:
                    raise KeyError(task_id)
                if task.owner is not None:
                    raise Refused(f"{task.status} by {task.owner}")
                raise Refused("blocked: a task it depends on is not done")
            return self._changed(conn, task_id)

    def heartbeat(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task:
        return self._held(task_id, agent, note, now, done=False)

    def finish(
        self, task_id: str, agent: str, note: str | None = None, now: datetime | None = None
    ) -> Task:
        return self._held(task_id, agent, note, now, done=True)

    def expire(self, now: datetime | None = None) -> list[Task]:
        at = now or datetime.now(UTC)
        ts, cutoff = _iso(at), _iso(at - HEARTBEAT_TIMEOUT)
        with self._write() as conn:
            stale = conn.execute(
                "SELECT id, owner, heartbeat_at FROM tasks"
                " WHERE status = 'claimed' AND heartbeat_at < ?",
                (cutoff,),
            ).fetchall()
            reopened: list[Task] = []
            for row in stale:
                _note(
                    conn,
                    row["id"],
                    SYSTEM,
                    f"Returned to open: {row['owner']} sent no heartbeat since"
                    f" {row['heartbeat_at']}.",
                    ts,
                )
                conn.execute(
                    "UPDATE tasks SET status = 'open', owner = NULL, heartbeat_at = NULL,"
                    " updated_at = ? WHERE id = ?",
                    (ts, row["id"]),
                )
                reopened.append(self._changed(conn, row["id"]))
            return reopened

    def delete_world(self, world_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM tasks WHERE world_id = ?", (world_id,))
            conn.execute("DELETE FROM verdicts WHERE world_id = ?", (world_id,))
            conn.execute("DELETE FROM agents WHERE world_id = ?", (world_id,))

    # Verdicts

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
        with self._write() as conn:
            (loop,) = conn.execute(
                "SELECT count(*) + 1 FROM verdicts WHERE stage_id = ?", (stage_id,)
            ).fetchone()
            verdict = Verdict(
                new_id("verdict"),
                world_id,
                run_id,
                stage_id,
                loop,
                passed,
                reasons,
                gaps,
                items,
                path,
                _iso(None),
            )
            conn.execute(
                "INSERT INTO verdicts (id, world_id, run_id, stage_id, loop, passed, reasons,"
                " gaps, items, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict.id,
                    world_id,
                    run_id,
                    stage_id,
                    loop,
                    int(passed),
                    json.dumps(reasons),
                    json.dumps(gaps),
                    json.dumps(items),
                    path,
                    verdict.created_at,
                ),
            )
            for gap in gaps:
                task_id = _insert_task(conn, world_id, run_id, stage_id, gap, added_by, "", ())
                self._changed(conn, task_id)
            event = Event.new(
                "verdict.created", world_id=world_id, run_id=run_id, payload=asdict(verdict)
            )
            append(conn, event)
            return verdict

    def list_verdicts(self, stage_id: str) -> list[Verdict]:
        with closing(connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM verdicts WHERE stage_id = ? ORDER BY loop", (stage_id,)
            ).fetchall()
        return [
            Verdict(
                r["id"],
                r["world_id"],
                r["run_id"],
                r["stage_id"],
                r["loop"],
                bool(r["passed"]),
                json.loads(r["reasons"]),
                json.loads(r["gaps"]),
                json.loads(r["items"]),
                r["path"],
                r["created_at"],
            )
            for r in rows
        ]

    # Agents and the live log

    def agent(self, world_id: str, run_id: str, stage_id: str, name: str, role: AgentRole) -> Agent:
        now = _iso(None)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO agents (id, world_id, run_id, stage_id, name, role, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'working', ?, ?)"
                " ON CONFLICT (stage_id, name) DO NOTHING",
                (new_id("agent"), world_id, run_id, stage_id, name, role, now, now),
            )
            return self._agent_changed(conn, stage_id, name)

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
        with self._write() as conn:
            found = conn.execute(
                "UPDATE agents SET status = coalesce(?, status),"
                " session_id = coalesce(?, session_id), cost_usd = cost_usd + coalesce(?, 0),"
                " skills = coalesce(?, skills), updated_at = ? WHERE stage_id = ? AND name = ?",
                (
                    status,
                    session_id,
                    cost_usd,
                    None if skills is None else json.dumps(skills),
                    _iso(None),
                    stage_id,
                    name,
                ),
            ).rowcount
            if not found:
                raise KeyError(name)
            return self._agent_changed(conn, stage_id, name)

    def list_agents(self, run_id: str) -> list[Agent]:
        with closing(connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM agents WHERE run_id = ? ORDER BY created_at, name", (run_id,)
            ).fetchall()
            return [_agent(r) for r in rows]

    def log(
        self,
        stage_id: str,
        agent: str,
        kind: LogKind,
        detail: str = "",
        tool: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> LogEntry:
        with self._write() as conn:
            row = conn.execute(
                "SELECT id, world_id, run_id FROM agents WHERE stage_id = ? AND name = ?",
                (stage_id, agent),
            ).fetchone()
            if row is None:
                raise KeyError(agent)
            ts = _iso(None)
            seq = conn.execute(
                "INSERT INTO agent_log (agent_id, stage_id, agent, kind, tool, input, detail, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    stage_id,
                    agent,
                    kind,
                    tool,
                    None if input is None else json.dumps(input),
                    detail,
                    ts,
                ),
            ).lastrowid
            assert seq is not None
            entry = LogEntry(seq, agent, stage_id, kind, ts, tool, input, detail)
            event = Event.new(
                "agent.logged",
                world_id=row["world_id"],
                run_id=row["run_id"],
                payload=asdict(entry),
            )
            append(conn, event)
            return entry

    def list_log(self, stage_id: str) -> list[LogEntry]:
        with closing(connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_log WHERE stage_id = ? ORDER BY seq", (stage_id,)
            ).fetchall()
        return [
            LogEntry(
                r["seq"],
                r["agent"],
                r["stage_id"],
                r["kind"],
                r["ts"],
                r["tool"],
                None if r["input"] is None else json.loads(r["input"]),
                r["detail"],
            )
            for r in rows
        ]

    @staticmethod
    def _agent_changed(conn: sqlite3.Connection, stage_id: str, name: str) -> Agent:
        row = conn.execute(
            "SELECT * FROM agents WHERE stage_id = ? AND name = ?", (stage_id, name)
        ).fetchone()
        agent = _agent(row)
        event = Event.new(
            "agent.updated", world_id=agent.world_id, run_id=agent.run_id, payload=asdict(agent)
        )
        append(conn, event)
        return agent

    def _held(
        self, task_id: str, agent: str, note: str | None, now: datetime | None, *, done: bool
    ) -> Task:
        """Refresh or finish a task `agent` holds, attaching `note`."""
        ts = _iso(now)
        status, heartbeat = ("done", None) if done else ("claimed", ts)
        with self._write() as conn:
            found = conn.execute(
                "UPDATE tasks SET status = ?, heartbeat_at = ?, updated_at = ?"
                " WHERE id = ? AND owner = ? AND status = 'claimed'",
                (status, heartbeat, ts, task_id, agent),
            ).rowcount
            if not found:
                task = _read(conn, task_id)
                if task is None:
                    raise KeyError(task_id)
                raise Refused(f"{agent} does not hold this task ({task.status})")
            if note and note.strip():
                _note(conn, task_id, agent, note.strip(), ts)
            return self._changed(conn, task_id)

    @staticmethod
    def _changed(conn: sqlite3.Connection, task_id: str) -> Task:
        """Read the task back and record its `task.updated` event in the open transaction."""
        task = _read(conn, task_id)
        assert task is not None
        event = Event.new(
            "task.updated", world_id=task.world_id, run_id=task.run_id, payload=asdict(task)
        )
        append(conn, event)
        return task


def _agent(row: sqlite3.Row) -> Agent:
    return Agent(
        row["id"],
        row["world_id"],
        row["run_id"],
        row["stage_id"],
        row["name"],
        row["role"],
        row["status"],
        row["session_id"],
        row["cost_usd"],
        row["created_at"],
        row["updated_at"],
        json.loads(row["skills"]),
    )


def _insert_task(
    conn: sqlite3.Connection,
    world_id: str,
    run_id: str,
    stage_id: str,
    title: str,
    added_by: str,
    body: str,
    depends_on: tuple[str, ...],
) -> str:
    title = title.strip()
    if not title:
        raise Refused("a task needs a title")
    for dep in dict.fromkeys(depends_on):
        row = conn.execute("SELECT stage_id FROM tasks WHERE id = ?", (dep,)).fetchone()
        if row is None or row["stage_id"] != stage_id:
            raise Refused(f"{dep} is not a task of this stage")
    now = _iso(None)
    task_id = new_id("task")
    conn.execute(
        "INSERT INTO tasks (id, world_id, run_id, stage_id, title, body, status, owner,"
        " added_by, heartbeat_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL, ?, ?)",
        (task_id, world_id, run_id, stage_id, title, body, added_by, now, now),
    )
    conn.executemany(
        "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
        [(task_id, dep) for dep in dict.fromkeys(depends_on)],
    )
    return task_id


def _note(conn: sqlite3.Connection, task_id: str, author: str, body: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO task_notes (task_id, author, body, ts) VALUES (?, ?, ?, ?)",
        (task_id, author, body, ts),
    )


def _read(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    deps = conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY depends_on", (task_id,)
    ).fetchall()
    notes = conn.execute(
        "SELECT author, body, ts FROM task_notes WHERE task_id = ? ORDER BY seq", (task_id,)
    ).fetchall()
    return Task(
        row["id"],
        row["world_id"],
        row["run_id"],
        row["stage_id"],
        row["title"],
        row["body"],
        row["status"],
        row["owner"],
        row["added_by"],
        row["heartbeat_at"],
        row["created_at"],
        row["updated_at"],
        [d[0] for d in deps],
        [Note(n["author"], n["body"], n["ts"]) for n in notes],
    )
