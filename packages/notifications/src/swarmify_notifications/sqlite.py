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


PUSH_COLUMNS = "id, label, gates, failures, budget, notices, created_at, last_success_at"


class PushStorage:
    """The Web Push subscriptions, one per browser. Rows are plain dicts; push.py shapes them."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.path))

    def save(self, id: str, endpoint: str, p256dh: str, auth: str, label: str) -> None:
        """Insert, or refresh the keys and label of the same endpoint; its toggles stay."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO push_subscriptions (id, endpoint, p256dh, auth, label, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (endpoint) DO UPDATE SET"
                " p256dh = excluded.p256dh, auth = excluded.auth, label = excluded.label",
                (id, endpoint, p256dh, auth, label, datetime.now(UTC).isoformat()),
            )

    def devices(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {PUSH_COLUMNS} FROM push_subscriptions ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def targets(self, kind: str | None) -> list[dict]:
        """Every subscription with its keys, or those with `kind` (a column name) switched on."""
        where = f" WHERE {kind} = 1" if kind else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, endpoint, p256dh, auth FROM push_subscriptions{where}"
            ).fetchall()
            return [dict(r) for r in rows]

    def toggle(self, id: str, kinds: dict[str, bool]) -> bool:
        """Set the given kinds (trusted column names) on or off. False when there is no such id."""
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM push_subscriptions WHERE id = ?", (id,)).fetchone():
                return False
            for kind, on in kinds.items():
                conn.execute(f"UPDATE push_subscriptions SET {kind} = ? WHERE id = ?", (on, id))
            return True

    def delete(self, id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (id,)).rowcount > 0

    def delivered(self, id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE push_subscriptions SET last_success_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), id),
            )
