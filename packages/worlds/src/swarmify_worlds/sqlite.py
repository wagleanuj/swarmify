"""SQLite adapter of the Worlds Storage port. One short connection per call.

The schema is not this adapter's job: the composition root runs `migrate` once with every
package's `MIGRATIONS` folder before constructing it.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from swarmify_kernel import Event
from swarmify_kernel.events import append, last_seq, read_after
from swarmify_kernel.sqlite import connect
from swarmify_worlds.model import MODEL_ROLES, CastEdit, Skin, World
from swarmify_worlds.packs import Casting

MIGRATIONS = Path(__file__).parent / "migrations"
SETTINGS = (
    "retention_days",
    "monthly_budget_usd",
    "run_cap",
    "agent_count",
    "model",
    *(f"{role}_model" for role in MODEL_ROLES),
    "skin",
)
"""The columns `update_settings` may write."""


def _world(row: sqlite3.Row) -> World:
    return World(
        row["id"],
        row["name"],
        row["purpose"],
        row["created_at"],
        row["retention_days"],
        row["monthly_budget_usd"],
        row["run_cap"],
        row["agent_count"],
        *(row[k] for k in SETTINGS[4:-1]),
        skin=_skin(json.loads(row["skin"])) if row["skin"] else Skin(),
    )


def _skin(data: dict[str, Any]) -> Skin:
    cast = {name: Casting(**look) for name, look in data.pop("cast", {}).items()}
    return Skin(**data, cast=cast)


def _edit(row: sqlite3.Row) -> CastEdit:
    tools = None if row["tools"] is None else json.loads(row["tools"])
    return CastEdit(row["name"], row["number"], row["prompt"], tools, row["created_at"])


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_world(self, world: World, event: Event) -> Event:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO worlds (id, name, purpose, created_at) VALUES (?, ?, ?, ?)",
                (world.id, world.name, world.purpose, world.created_at),
            )
            stored = append(conn, event)
            conn.execute("COMMIT")
            return stored

    def list_worlds(self) -> list[World]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM worlds ORDER BY created_at, id").fetchall()
            return [_world(r) for r in rows]

    def get_world(self, world_id: str) -> World | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
            return _world(row) if row else None

    def delete_world(self, world_id: str, event: Event) -> bool:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM worlds WHERE id = ?", (world_id,)).rowcount == 1
            if deleted:
                conn.execute("DELETE FROM cast_edits WHERE world_id = ?", (world_id,))
                append(conn, event)
            conn.execute("COMMIT")
            return deleted

    def update_settings(self, world_id: str, changes: dict[str, Any], event: Event) -> World | None:
        unknown = set(changes) - set(SETTINGS)
        if unknown:
            raise ValueError(f"not a setting: {', '.join(sorted(unknown))}")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if changes:
                assignments = ", ".join(f"{k} = ?" for k in changes)
                values = [json.dumps(v) if k == "skin" else v for k, v in changes.items()]
                found = conn.execute(
                    f"UPDATE worlds SET {assignments} WHERE id = ?", (*values, world_id)
                ).rowcount
                if found:
                    append(conn, event)
            conn.execute("COMMIT")
        return self.get_world(world_id)

    def cast_edits(self, world_id: str, name: str | None = None) -> list[CastEdit]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cast_edits WHERE world_id = ? AND (? IS NULL OR name = ?)"
                " ORDER BY name, number",
                (world_id, name, name),
            ).fetchall()
            return [_edit(r) for r in rows]

    def add_cast_edit(self, world_id: str, edit: CastEdit, event: Event) -> CastEdit:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO cast_edits (world_id, name, number, prompt, tools, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    world_id,
                    edit.name,
                    edit.number,
                    edit.prompt,
                    None if edit.tools is None else json.dumps(edit.tools),
                    edit.created_at,
                ),
            )
            append(conn, event)
            conn.execute("COMMIT")
            return edit

    def events_after(self, world_id: str | None, after_seq: int) -> list[Event]:
        with self._conn() as conn:
            return read_after(conn, world_id, after_seq)

    def last_seq(self) -> int:
        with self._conn() as conn:
            return last_seq(conn)
