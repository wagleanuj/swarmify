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
from swarmify_runs.machine import MAX_SEND_BACKS, Illegal, RunState
from swarmify_runs.model import Decision, Gate, Run, RunStageView, StageDef

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
                " created_at, updated_at, parent_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.world_id,
                    run.workflow_id,
                    run.workflow_version,
                    run.name,
                    run.status,
                    run.created_at,
                    run.updated_at,
                    run.parent_run_id,
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
                "UPDATE runs SET status = ?, send_backs = ?, updated_at = ? WHERE id = ?",
                (state.status, state.send_backs, now, run_id),
            ).rowcount
            if not found:
                conn.execute("ROLLBACK")
                return None
            for i, status in enumerate(state.stages):
                # A stage sent back to pending forgets its timestamps, so it reads as fresh.
                conn.execute(
                    "UPDATE run_stages SET status = :s,"
                    " started_at = CASE WHEN :s = 'pending' THEN NULL"
                    "   WHEN :s IN ('running', 'waiting') THEN coalesce(started_at, :now)"
                    "   ELSE started_at END,"
                    " ended_at = CASE WHEN :s = 'pending' THEN NULL"
                    "   WHEN :s IN ('done', 'failed', 'skipped') THEN coalesce(ended_at, :now)"
                    "   ELSE ended_at END,"
                    " error = CASE WHEN :s = 'failed' THEN coalesce(:error, error) ELSE error END"
                    " WHERE run_id = :run AND idx = :i AND status != :s",
                    {"s": status, "now": now, "error": error, "run": run_id, "i": i},
                )
            saved = self._read(conn, run_id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def add_decision(self, run_id: str, decision: Decision, visit: int) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO gate_decisions (id, run_id, stage_index, stage_name, kind, choice,"
                    " note, send_back_to, path, decided_at, visit)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.id,
                        run_id,
                        decision.stage_index,
                        decision.stage_name,
                        decision.kind,
                        decision.choice,
                        decision.note,
                        decision.send_back_to,
                        decision.path,
                        decision.decided_at,
                        visit,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Illegal("the gate was already decided") from exc

    def record(self, event: Event) -> None:
        with self._conn() as conn:
            append(conn, event)

    def get_run(self, run_id: str) -> Run | None:
        with self._conn() as conn:
            return self._read(conn, run_id)

    def open_gates(self) -> list[Gate]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.id, s.idx, s.name, s.config, s.started_at, s.run_id,"
                " r.world_id, r.name AS run_name, r.send_backs, r.parent_run_id"
                " FROM run_stages s JOIN runs r ON r.id = s.run_id"
                " WHERE s.status = 'waiting' AND s.type = 'gate'"
                " ORDER BY s.started_at DESC, s.id DESC"
            ).fetchall()
            gates = []
            for g in rows:
                earlier = conn.execute(
                    "SELECT name FROM run_stages WHERE run_id = ? AND idx < ? ORDER BY idx",
                    (g["run_id"], g["idx"]),
                ).fetchall()
                config = json.loads(g["config"])
                gates.append(
                    Gate(
                        g["run_id"],
                        g["world_id"],
                        g["run_name"],
                        g["id"],
                        g["idx"],
                        g["name"],
                        config.get("kind") or "approve",
                        config.get("ask") or "",
                        g["started_at"] or "",
                        MAX_SEND_BACKS - g["send_backs"],
                        [e["name"] for e in earlier],
                        g["parent_run_id"],
                    )
                )
            return gates

    def child_run_ids(self, run_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM runs WHERE parent_run_id = ?", (run_id,))
            return [r[0] for r in rows]

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
        decisions = [
            Decision(
                d["id"],
                d["stage_index"],
                d["stage_name"],
                d["kind"],
                d["choice"],
                d["note"],
                d["send_back_to"],
                d["path"],
                d["decided_at"],
            )
            for d in conn.execute(
                "SELECT * FROM gate_decisions WHERE run_id = ? ORDER BY decided_at, id", (run_id,)
            ).fetchall()
        ]
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
                    [d.note for d in decisions if d.send_back_to == s["name"] and d.note],
                )
                for s in stages
            ],
            row["send_backs"],
            row["parent_run_id"],
            decisions,
        )


def _updated(run: Run) -> Event:
    """One event type carrying the whole run, so any skin renders it without a refetch."""
    return Event.new("run.updated", world_id=run.world_id, run_id=run.id, payload=asdict(run))
