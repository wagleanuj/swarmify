"""SQLite adapter of the Swarm Storage port. One short connection per call; never migrates.

Every write is one `BEGIN IMMEDIATE` transaction: it takes SQLite's single write lock up front,
so a claim's check and update cannot interleave with another connection's (ADR 0017).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_swarm.model import HEARTBEAT_TIMEOUT, Note, Refused, Task

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
        title = title.strip()
        if not title:
            raise Refused("a task needs a title")
        now = _iso(None)
        task_id = new_id("task")
        with self._write() as conn:
            for dep in dict.fromkeys(depends_on):
                row = conn.execute("SELECT stage_id FROM tasks WHERE id = ?", (dep,)).fetchone()
                if row is None or row["stage_id"] != stage_id:
                    raise Refused(f"{dep} is not a task of this stage")
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
            return self._changed(conn, task_id)

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
