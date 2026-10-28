"""Event envelope and the append-only event log every context writes to (ADR 0002)."""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from swarmify_kernel.ids import new_id


@dataclass(frozen=True)
class Event:
    id: str
    world_id: str
    type: str
    payload: dict[str, Any]
    ts: str
    run_id: str | None = None
    seq: int | None = None

    @staticmethod
    def new(
        type: str, *, world_id: str, payload: dict[str, Any], run_id: str | None = None
    ) -> "Event":
        return Event(new_id("evt"), world_id, type, payload, datetime.now(UTC).isoformat(), run_id)


def append(conn: sqlite3.Connection, event: Event) -> Event:
    """Insert `event` in the caller's transaction and return it with its `seq`."""
    cur = conn.execute(
        "INSERT INTO events (id, world_id, run_id, ts, type, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (event.id, event.world_id, event.run_id, event.ts, event.type, json.dumps(event.payload)),
    )
    return replace(event, seq=cur.lastrowid)


def last_seq(conn: sqlite3.Connection) -> int:
    """The newest event's `seq` across every world, 0 when the log is empty."""
    return int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])


def read_after(conn: sqlite3.Connection, world_id: str | None, after_seq: int) -> list[Event]:
    """Events after `after_seq` in order: one world's, or every world's when `world_id` is None.
    `seq` is one counter across worlds, so either stream resumes from a single Last-Event-ID."""
    where, args = (
        ("seq > ?", (after_seq,))
        if world_id is None
        else (
            "world_id = ? AND seq > ?",
            (world_id, after_seq),
        )
    )
    rows = conn.execute(
        "SELECT seq, id, world_id, run_id, ts, type, payload FROM events"
        f" WHERE {where} ORDER BY seq",
        args,
    ).fetchall()
    return [
        Event(
            r["id"],
            r["world_id"],
            r["type"],
            json.loads(r["payload"]),
            r["ts"],
            r["run_id"],
            r["seq"],
        )
        for r in rows
    ]
