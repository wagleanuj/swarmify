"""SQLite adapter of the Workflows Storage port. One short connection per call; never migrates."""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_workflows.model import Workflow, WorkflowVersion
from swarmify_workflows.parse import parse_workflow

MIGRATIONS = Path(__file__).parent / "migrations"


def _workflow(row: sqlite3.Row) -> Workflow:
    return Workflow(
        row["id"], row["world_id"], row["name"], row["current_version"], row["created_at"]
    )


def _version(row: sqlite3.Row) -> WorkflowVersion:
    model, _ = parse_workflow(row["yaml"])
    assert model is not None, "stored YAML was validated on save"
    return WorkflowVersion(
        row["id"],
        row["workflow_id"],
        row["number"],
        row["yaml"],
        row["created_at"],
        model,
        json.loads(row["packages"]),
    )


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_workflow(
        self, workflow: Workflow, yaml: str, event: Event, packages: Mapping[str, str] | None = None
    ) -> WorkflowVersion:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO workflows (id, world_id, name, current_version, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                (workflow.id, workflow.world_id, workflow.name, workflow.created_at),
            )
            version = self._insert_version(
                conn, workflow.id, 1, yaml, workflow.created_at, packages
            )
            append(conn, event)
            conn.execute("COMMIT")
            return version

    def add_version(
        self,
        workflow_id: str,
        yaml: str,
        name: str,
        event: Event,
        packages: Mapping[str, str] | None = None,
    ) -> WorkflowVersion | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "UPDATE workflows SET current_version = current_version + 1, name = ?"
                " WHERE id = ? RETURNING current_version",
                (name, workflow_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            now = datetime.now(UTC).isoformat()
            version = self._insert_version(conn, workflow_id, row[0], yaml, now, packages)
            append(conn, event)
            conn.execute("COMMIT")
            return version

    @staticmethod
    def _insert_version(
        conn: sqlite3.Connection,
        workflow_id: str,
        number: int,
        yaml: str,
        created_at: str,
        packages: Mapping[str, str] | None,
    ) -> WorkflowVersion:
        version_id = new_id("wfv")
        conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, number, yaml, created_at, packages)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, workflow_id, number, yaml, created_at, json.dumps(dict(packages or {}))),
        )
        row = conn.execute("SELECT * FROM workflow_versions WHERE id = ?", (version_id,)).fetchone()
        return _version(row)

    def list_workflows(self, world_id: str) -> list[Workflow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflows WHERE world_id = ? ORDER BY created_at, id", (world_id,)
            ).fetchall()
            return [_workflow(r) for r in rows]

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            return _workflow(row) if row else None

    def list_versions(self, workflow_id: str) -> list[WorkflowVersion]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? ORDER BY number",
                (workflow_id,),
            ).fetchall()
            return [_version(r) for r in rows]

    def get_version(self, workflow_id: str, number: int) -> WorkflowVersion | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND number = ?",
                (workflow_id, number),
            ).fetchone()
            return _version(row) if row else None

    def delete_world(self, world_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM workflows WHERE world_id = ?", (world_id,))

    def current_yaml(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT w.id, v.yaml FROM workflows w JOIN workflow_versions v"
                " ON v.workflow_id = w.id AND v.number = w.current_version"
            ).fetchall()
            return {r["id"]: r["yaml"] for r in rows}

    def all_versions(self) -> list[tuple[str, WorkflowVersion]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT w.world_id, v.* FROM workflow_versions v JOIN workflows w"
                " ON w.id = v.workflow_id ORDER BY w.created_at, w.id, v.number"
            ).fetchall()
            return [(r["world_id"], _version(r)) for r in rows]

    def set_packages(self, version_id: str, packages: Mapping[str, str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE workflow_versions SET packages = ? WHERE id = ?",
                (json.dumps(dict(packages)), version_id),
            )
