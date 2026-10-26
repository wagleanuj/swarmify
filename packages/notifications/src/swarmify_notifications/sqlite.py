"""SQLite adapter of the Notifications Storage port. One short connection per call."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel.sqlite import connect

MIGRATIONS = Path(__file__).parent / "migrations"


class SqliteStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def get_webhook(self, world_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT slack_webhook FROM channels WHERE world_id = ?", (world_id,)
            ).fetchone()
            return row["slack_webhook"] if row else None

    def set_webhook(self, world_id: str, url: str | None) -> None:
        with self._conn() as conn:
            if url is None:
                conn.execute("DELETE FROM channels WHERE world_id = ?", (world_id,))
                return
            conn.execute(
                "INSERT INTO channels (world_id, slack_webhook, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (world_id) DO UPDATE SET"
                " slack_webhook = excluded.slack_webhook, updated_at = excluded.updated_at",
                (world_id, url, datetime.now(UTC).isoformat()),
            )
