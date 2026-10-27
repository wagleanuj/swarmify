"""SQLite adapter of the Learning Storage port. One short connection per call; never migrates."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_learning.model import Proposal, SkillChange, SkillVersion, WorkflowSuggestion

MIGRATIONS = Path(__file__).parent / "migrations"


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_version(self, version: SkillVersion, event: Event) -> SkillVersion:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO skill_versions (id, world_id, name, number, content, run_id, note,"
                " retired, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version.id,
                    version.world_id,
                    version.name,
                    version.number,
                    version.content,
                    version.run_id,
                    version.note,
                    int(version.retired),
                    version.created_at,
                ),
            )
            append(conn, event)
            conn.execute("COMMIT")
            return version

    def versions(self, world_id: str, name: str | None = None) -> list[SkillVersion]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE world_id = ? AND (? IS NULL OR name = ?)"
                " ORDER BY name, number",
                (world_id, name, name),
            ).fetchall()
            return [
                SkillVersion(
                    r["id"],
                    r["world_id"],
                    r["name"],
                    r["number"],
                    r["content"],
                    r["run_id"],
                    r["note"],
                    bool(r["retired"]),
                    r["created_at"],
                )
                for r in rows
            ]

    def add_proposal(self, proposal: Proposal, event: Event) -> Proposal:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO skill_proposals (id, world_id, run_id, stage_id, skill, workflow,"
                " status, note, path, created_at, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.id,
                    proposal.world_id,
                    proposal.run_id,
                    proposal.stage_id,
                    json.dumps(asdict(proposal.skill)) if proposal.skill else None,
                    json.dumps(asdict(proposal.workflow)) if proposal.workflow else None,
                    proposal.status,
                    proposal.note,
                    proposal.path,
                    proposal.created_at,
                    proposal.decided_at,
                ),
            )
            append(conn, event)
            conn.execute("COMMIT")
            return proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return _proposal(row) if row else None

    def pending(self, run_id: str) -> Proposal | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM skill_proposals WHERE run_id = ? AND status = 'pending'"
                " ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return _proposal(row) if row else None

    def list_proposals(self, world_id: str) -> list[Proposal]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_proposals WHERE world_id = ? ORDER BY created_at, id",
                (world_id,),
            ).fetchall()
            return [_proposal(r) for r in rows]

    def decide_proposal(
        self, proposal_id: str, status: str, note: str, decided_at: str, event: Event
    ) -> Proposal | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE skill_proposals SET status = ?, note = ?, decided_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, note, decided_at, proposal_id),
            ).rowcount
            if not changed:
                conn.execute("ROLLBACK")
                return None
            append(conn, event)
            row = conn.execute(
                "SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return _proposal(row)

    def delete_world(self, world_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM skill_versions WHERE world_id = ?", (world_id,))
            conn.execute("DELETE FROM skill_proposals WHERE world_id = ?", (world_id,))


def _proposal(row: sqlite3.Row) -> Proposal:
    skill = json.loads(row["skill"]) if row["skill"] else None
    workflow = json.loads(row["workflow"]) if row["workflow"] else None
    return Proposal(
        row["id"],
        row["world_id"],
        row["run_id"],
        row["stage_id"],
        SkillChange(**skill) if skill else None,
        WorkflowSuggestion(**workflow) if workflow else None,
        row["status"],
        row["note"],
        row["path"],
        row["created_at"],
        row["decided_at"],
    )
