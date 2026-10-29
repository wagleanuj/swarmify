"""SQLite adapter of the Runs Storage port. One short connection per call; never migrates."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_runs.machine import MAX_SEND_BACKS, Illegal, RunState
from swarmify_runs.model import Decision, Gate, Run, RunLink, RunStageView, StageDef

MIGRATIONS = Path(__file__).parent / "migrations"
ENDED = ("done", "failed", "aborted")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_run(self, run: Run, stages: list[StageDef], skipped: int = 0) -> Run:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_run(conn, run)
            for i, stage in enumerate(stages):
                conn.execute(
                    "INSERT INTO run_stages (id, run_id, idx, name, type, config, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("stage"),
                        run.id,
                        i,
                        stage.name,
                        stage.type,
                        json.dumps(stage.config),
                        "skipped" if i < skipped else "pending",
                    ),
                )
            saved = self._read(conn, run.id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def fork(self, run: Run, source: Run, index: int) -> Run:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_run(conn, run, source.id, index)
            stages = conn.execute(
                "SELECT * FROM run_stages WHERE run_id = ? ORDER BY idx", (source.id,)
            ).fetchall()
            renamed = {}
            for s in stages:
                copied = s["idx"] < index
                renamed[s["id"]] = new_id("stage")
                conn.execute(
                    "INSERT INTO run_stages (id, run_id, idx, name, type, config, status,"
                    " started_at, ended_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        renamed[s["id"]],
                        run.id,
                        s["idx"],
                        s["name"],
                        s["type"],
                        s["config"],
                        s["status"] if copied else "pending",
                        s["started_at"] if copied else None,
                        s["ended_at"] if copied else None,
                        s["error"] if copied else None,
                    ),
                )
            decisions = conn.execute(
                "SELECT * FROM gate_decisions WHERE run_id = ? AND stage_index < ?"
                " ORDER BY decided_at, id",
                (source.id, index),
            ).fetchall()
            for d in decisions:
                # The file was copied along with its stage folder, renamed to the fork's stage
                # id; a copied visit is numbered below zero so it never collides with a visit
                # the fork makes itself.
                old_stage = stages[d["stage_index"]]["id"]
                path = d["path"].replace(source.id, run.id).replace(old_stage, renamed[old_stage])
                artifact = d["artifact"] and d["artifact"].replace(source.id, run.id)
                for old, new in renamed.items():  # the artifact may be from any earlier stage
                    artifact = artifact and artifact.replace(old, new)
                conn.execute(
                    "INSERT INTO gate_decisions (id, run_id, stage_index, stage_name, kind, choice,"
                    " note, send_back_to, path, decided_at, visit, artifact, publish)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("dec"),
                        run.id,
                        d["stage_index"],
                        d["stage_name"],
                        d["kind"],
                        d["choice"],
                        d["note"],
                        d["send_back_to"],
                        path,
                        d["decided_at"],
                        -1 - d["visit"],
                        artifact,
                        d["publish"],
                    ),
                )
            saved = self._read(conn, run.id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    @staticmethod
    def _insert_run(
        conn: sqlite3.Connection, run: Run, forked_from: str | None = None, index: int | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO runs (id, world_id, workflow_id, workflow_version, name, status,"
            " created_at, updated_at, parent_run_id, forked_from_run_id, fork_stage_index,"
            " promoted_from, budget_usd, baseline)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.world_id,
                run.workflow_id or "",  # an ad hoc run has no workflow; the column is NOT NULL
                run.workflow_version or 0,
                run.name,
                run.status,
                run.created_at,
                run.updated_at,
                run.parent_run_id,
                forked_from,
                index,
                run.promoted_from,
                run.budget_usd,
                run.baseline,
            ),
        )

    def save_state(self, run_id: str, state: RunState, error: str | None = None) -> Run | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            found = conn.execute(
                "UPDATE runs SET status = ?, send_backs = ?, over_budget = ?, updated_at = ?"
                " WHERE id = ?",
                (state.status, state.send_backs, state.over_budget, now, run_id),
            ).rowcount
            if not found:
                conn.execute("ROLLBACK")
                return None
            for i, status in enumerate(state.stages):
                # A stage sent back forgets its timestamps and its child run, so it reads as
                # fresh. A send-back usually reruns the stage at once, so it may never be saved
                # as pending: an ended stage turning active again is a fresh visit too.
                conn.execute(
                    "UPDATE run_stages SET status = :s,"
                    " started_at = CASE WHEN :s = 'pending' THEN NULL"
                    "   WHEN :s IN ('running', 'waiting') AND ended_at IS NOT NULL THEN :now"
                    "   WHEN :s IN ('running', 'waiting') THEN coalesce(started_at, :now)"
                    "   ELSE started_at END,"
                    " ended_at = CASE WHEN :s IN ('pending', 'running', 'waiting') THEN NULL"
                    "   ELSE coalesce(ended_at, :now) END,"
                    " error = CASE WHEN :s = 'failed' THEN coalesce(:error, error) ELSE error END,"
                    " child_run_id = CASE WHEN :s IN ('pending', 'running') THEN NULL"
                    "   ELSE child_run_id END"
                    " WHERE run_id = :run AND idx = :i AND status != :s",
                    {"s": status, "now": now, "error": error, "run": run_id, "i": i},
                )
            saved = self._read(conn, run_id)
            assert saved is not None
            append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def set_spent(self, run_id: str, usd: float) -> Run | None:
        return self._set(run_id, "spent_usd", usd)

    def set_budget(self, run_id: str, usd: float) -> Run | None:
        return self._set(run_id, "budget_usd", usd)

    def set_stop(self, run_id: str, stop: str | None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET stop = ? WHERE id = ?", (stop, run_id))

    def stop_of(self, run_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT stop FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row["stop"] if row else None

    def _set(self, run_id: str, column: str, usd: float) -> Run | None:
        """Write one money column; a change appends `run.updated`. None for an unknown run."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                f"UPDATE runs SET {column} = ? WHERE id = ? AND {column} IS NOT ?",
                (usd, run_id, usd),
            ).rowcount
            saved = self._read(conn, run_id)
            if changed and saved is not None:
                append(conn, _updated(saved))
            conn.execute("COMMIT")
            return saved

    def mark_warned(self, run_id: str, budget_usd: float) -> bool:
        with self._conn() as conn:
            return bool(
                conn.execute(
                    "UPDATE runs SET budget_warned_usd = ? WHERE id = ?"
                    " AND budget_warned_usd IS NOT ?",
                    (budget_usd, run_id, budget_usd),
                ).rowcount
            )

    def latest_baseline(self, workflow_id: str | None) -> Run | None:
        if workflow_id is None:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE workflow_id = ? AND baseline = 1 AND status = 'done'"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            return self._read(conn, row[0]) if row else None

    def spent_in_month(self, world_id: str, month: str) -> float:
        with self._conn() as conn:
            (total,) = conn.execute(
                "SELECT coalesce(sum(spent_usd), 0) FROM runs WHERE world_id = ?"
                " AND parent_run_id IS NULL AND created_at LIKE ?",
                (world_id, f"{month}-%"),
            ).fetchone()
            return float(total)

    def add_decision(self, run_id: str, decision: Decision, visit: int) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO gate_decisions (id, run_id, stage_index, stage_name, kind, choice,"
                    " note, send_back_to, path, decided_at, visit, artifact, publish)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        decision.artifact,
                        json.dumps(decision.publish) if decision.publish else None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Illegal("the gate was already decided") from exc

    def set_child(self, run_id: str, index: int, child_run_id: str) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE run_stages SET child_run_id = ? WHERE run_id = ? AND idx = ?",
                (child_run_id, run_id, index),
            )
            saved = self._read(conn, run_id)
            if saved is not None:
                append(conn, _updated(saved))
            conn.execute("COMMIT")

    def remove_decision(self, decision_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM gate_decisions WHERE id = ?", (decision_id,))

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
                " r.world_id, r.name AS run_name, r.parent_run_id, r.over_budget"
                " FROM run_stages s JOIN runs r ON r.id = s.run_id"
                " WHERE s.status = 'waiting' AND (s.type IN ('gate', 'retro') OR r.over_budget = 1)"
                " ORDER BY s.started_at DESC, s.id DESC"
            ).fetchall()
            gates = []
            for g in rows:
                earlier = conn.execute(
                    "SELECT name FROM run_stages WHERE run_id = ? AND idx < ? ORDER BY idx",
                    (g["run_id"], g["idx"]),
                ).fetchall()
                config = json.loads(g["config"])
                limit = int(config.get("max_send_backs") or MAX_SEND_BACKS)
                (sent,) = conn.execute(
                    "SELECT count(*) FROM gate_decisions WHERE run_id = ? AND stage_index = ?"
                    " AND send_back_to IS NOT NULL",
                    (g["run_id"], g["idx"]),
                ).fetchone()
                gate = Gate(
                    g["run_id"],
                    g["world_id"],
                    g["run_name"],
                    g["id"],
                    g["idx"],
                    g["name"],
                    config.get("kind") or "approve",
                    config.get("ask") or "",
                    g["started_at"] or "",
                    max(limit - sent, 0),
                    [e["name"] for e in earlier],
                    g["parent_run_id"],
                    _link(conn, g["parent_run_id"]),
                    max_send_backs=limit,
                )
                if g["over_budget"]:  # RunControl.gates adds the money from the top run
                    gate = replace(gate, kind="budget", send_backs_left=0, earlier_stages=[])
                gates.append(gate)
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
                d["artifact"],
                json.loads(d["publish"]) if d["publish"] else None,
            )
            for d in conn.execute(
                "SELECT * FROM gate_decisions WHERE run_id = ? ORDER BY decided_at, id", (run_id,)
            ).fetchall()
        ]
        return Run(
            row["id"],
            row["world_id"],
            row["workflow_id"] or None,
            row["workflow_version"] or None,
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
                    _link(conn, s["child_run_id"]),
                    json.loads(s["config"]).get("calls") if s["type"] == "connector" else None,
                    json.loads(s["config"]).get("ask") if s["type"] == "gate" else None,
                )
                for s in stages
            ],
            row["send_backs"],
            row["parent_run_id"],
            decisions,
            _link(conn, row["parent_run_id"]),
            _link(conn, row["forked_from_run_id"]),
            row["fork_stage_index"],
            # ponytail: one query per run read, so list_runs is N+1 (like _link); join if it shows
            [
                RunLink(f["id"], f["world_id"], f["name"])
                for f in conn.execute(
                    "SELECT id, world_id, name FROM runs WHERE forked_from_run_id = ?"
                    " ORDER BY created_at, id",
                    (run_id,),
                )
            ],
            row["promoted_from"],
            row["budget_usd"],
            row["spent_usd"],
            bool(row["over_budget"]),
            bool(row["baseline"]),
        )


def _link(conn: sqlite3.Connection, run_id: str | None) -> RunLink | None:
    if run_id is None:
        return None
    row = conn.execute("SELECT id, world_id, name FROM runs WHERE id = ?", (run_id,)).fetchone()
    return RunLink(row["id"], row["world_id"], row["name"]) if row else None


def _updated(run: Run) -> Event:
    """One event type carrying the whole run, so any skin renders it without a refetch."""
    return Event.new("run.updated", world_id=run.world_id, run_id=run.id, payload=asdict(run))
