"""SQLite adapter of the Worlds Storage port. One short connection per call.

The schema is not this adapter's job: the composition root runs `migrate` once with every
package's `MIGRATIONS` folder before constructing it.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from swarmify_kernel import Event
from swarmify_kernel.events import append, read_after
from swarmify_kernel.sqlite import connect
from swarmify_worlds.model import World

MIGRATIONS = Path(__file__).parent / "migrations"


def _world(row: sqlite3.Row) -> World:
    return World(row["id"], row["name"], row["purpose"], row["created_at"])


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
                append(conn, event)
            conn.execute("COMMIT")
            return deleted

    def events_after(self, world_id: str, after_seq: int) -> list[Event]:
        with self._conn() as conn:
            return read_after(conn, world_id, after_seq)
