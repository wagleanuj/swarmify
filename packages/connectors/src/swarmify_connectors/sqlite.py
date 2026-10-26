"""SQLite adapter of the Connectors Storage port. One short connection per call; never migrates."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from swarmify_connectors.model import Call, Connector
from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect

MIGRATIONS = Path(__file__).parent / "migrations"
_JSON = ("request", "response")


def _connector(row: sqlite3.Row) -> Connector:
    return Connector(
        row["id"],
        row["world_id"],
        row["name"],
        row["kind"],
        json.loads(row["command"]),
        row["url"],
        json.loads(row["credentials"]),
        json.loads(row["stage_types"]),
        created_at=row["created_at"],
    )


def _call(row: sqlite3.Row) -> Call:
    fields = dict(row)
    for key in _JSON:
        fields[key] = json.loads(fields[key]) if fields[key] is not None else None
    return Call(**fields)


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def add_connector(self, connector: Connector) -> None:
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO connectors (id, world_id, name, kind, command, url, credentials,"
                    " stage_types, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        connector.id,
                        connector.world_id,
                        connector.name,
                        connector.kind,
                        json.dumps(connector.command),
                        connector.url,
                        json.dumps(connector.credentials),
                        json.dumps(connector.stage_types),
                        connector.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" not in str(exc):
                    raise
                raise ValueError(f"a connector named '{connector.name}' already exists") from exc

    def list_connectors(self, world_id: str) -> list[Connector]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors WHERE world_id = ? ORDER BY created_at, id", (world_id,)
            )
            return [_connector(r) for r in rows]

    def get_connector(self, connector_id: str) -> Connector | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM connectors WHERE id = ?", (connector_id,)).fetchone()
            return _connector(row) if row else None

    def delete_connector(self, connector_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM connectors WHERE id = ?", (connector_id,)).rowcount > 0

    def get_call(self, idempotency_key: str) -> Call | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM connector_calls WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            return _call(row) if row else None

    def save_call(self, call: Call) -> Call:
        fields = asdict(call)
        for key in _JSON:
            fields[key] = json.dumps(fields[key]) if fields[key] is not None else None
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT OR REPLACE INTO connector_calls ({names}) VALUES ({marks})",
                tuple(fields.values()),
            )
            event = Event.new(
                "connector_call.updated",
                world_id=call.world_id,
                run_id=call.run_id,
                payload=asdict(call),
            )
            append(conn, event)
            conn.execute("COMMIT")
        return call

    def delete_world(self, world_id: str) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM connectors WHERE world_id = ?", (world_id,))
            conn.execute("DELETE FROM connector_calls WHERE world_id = ?", (world_id,))
            conn.execute("COMMIT")
