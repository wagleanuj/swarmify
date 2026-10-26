"""SQLite adapter of the Runs Storage port. One short connection per call; never migrates."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_runs.machine import RunState
from swarmify_runs.model import Run, RunStageView, StageDef

MIGRATIONS = Path(__file__).parent / "migrations"
ENDED = ("done", "failed", "aborted")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_run(self, run: Run, stages: list[StageDef]) -> Run:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO runs (id, world_id, workflow_id, workflow_version, name, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.world_id,
                    run.workflow_id,
                    run.workflow_version,
                    run.name,
                    run.status,
                    run.created_at,
                    run.updated_at,
                ),
            )
            for i, stage in enumerate(stages):
                conn.execute(
                    "INSERT INTO run_stages (id, run_id, idx, name, type, config, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (new_id("stage"), run.id, i, stage.name, stage.type, json.dumps(stage.config)),
                )
            saved = self._read(conn, run.id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def save_state(self, run_id: str, state: RunState, error: str | None = None) -> Run | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            found = conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (state.status, now, run_id),
            ).rowcount
            if not found:
                conn.execute("ROLLBACK")
                return None
            for i, status in enumerate(state.stages):
                conn.execute(
                    "UPDATE run_stages SET status = ?,"
                    " started_at = CASE WHEN ? IN ('running', 'waiting')"
                    "   THEN coalesce(started_at, ?) ELSE started_at END,"
                    " ended_at = CASE WHEN ? IN ('done', 'failed', 'aborted', 'skipped')"
                    "   THEN coalesce(ended_at, ?) ELSE ended_at END,"
                    " error = CASE WHEN ? = 'failed' THEN coalesce(?, error) ELSE error END"
                    " WHERE run_id = ? AND idx = ? AND status != ?",
                    (status, status, now, status, now, status, error, run_id, i, status),
                )
            saved = self._read(conn, run_id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def get_run(self, run_id: str) -> Run | None:
        with self._conn() as conn:
            return self._read(conn, run_id)

    def stage_def(self, run_id: str, index: int) -> tuple[str, StageDef]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, type, config FROM run_stages WHERE run_id = ? AND idx = ?",
                (run_id, index),
            ).fetchone()
            if row is None:
                raise KeyError(f"{run_id} stage {index}")
            return row["id"], StageDef(row["name"], row["type"], json.loads(row["config"]))

    def list_runs(self, world_id: str) -> list[Run]:
        with self._conn() as conn:
            ids = conn.execute(
                "SELECT id FROM runs WHERE world_id = ? ORDER BY created_at DESC, id DESC",
                (world_id,),
            ).fetchall()
            return [r for (run_id,) in ids if (r := self._read(conn, run_id))]

    def queued_run_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at")
            return [r[0] for r in rows]

    def active_run_ids(self, world_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE world_id = ? AND status NOT IN (?, ?, ?)",
                (world_id, *ENDED),
            )
            return [r[0] for r in rows]

    def delete_world(self, world_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM runs WHERE world_id = ?", (world_id,))

    @staticmethod
    def _read(conn: sqlite3.Connection, run_id: str) -> Run | None:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        stages = conn.execute(
            "SELECT * FROM run_stages WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
        return Run(
            row["id"],
            row["world_id"],
            row["workflow_id"],
            row["workflow_version"],
            row["name"],
            row["status"],
            row["created_at"],
            row["updated_at"],
            [
                RunStageView(
                    s["id"],
                    s["idx"],
                    s["name"],
                    s["type"],
                    s["status"],
                    s["started_at"],
                    s["ended_at"],
                    s["error"],
                )
                for s in stages
            ],
        )


def _updated(run: Run) -> Event:
    """One event type carrying the whole run, so any skin renders it without a refetch."""
    return Event.new("run.updated", world_id=run.world_id, run_id=run.id, payload=asdict(run))
