"""SQLite access: one connection factory and a migration runner.

Migrations are numbered SQL files, `NNNN_name.sql`, applied in order inside one transaction
each; the `migrations` table records every applied number, so a lower number merged after a
higher one still runs. Numbers are unique across the monorepo: each package ships its own
`migrations/` folder and the composition root passes them all to `migrate`.
"""

import re
import sqlite3
from pathlib import Path

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    """Open `path` with WAL, foreign keys, a 5 s busy timeout and dict-like rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection, *dirs: Path) -> list[int]:
    """Apply every migration not yet recorded in `migrations`. Returns the numbers applied."""
    files: dict[int, Path] = {}
    for file in sorted(f for d in dirs for f in d.glob("*.sql")):
        m = _NAME.match(file.name)
        if not m:
            raise ValueError(f"migration name must be NNNN_name.sql: {file}")
        number = int(m.group(1))
        if number in files:
            raise ValueError(f"duplicate migration number {m.group(1)}: {files[number]}, {file}")
        files[number] = file
    conn.execute("CREATE TABLE IF NOT EXISTS migrations (number INTEGER PRIMARY KEY)")
    done = {row[0] for row in conn.execute("SELECT number FROM migrations")}
    if not done:
        # Databases from before the table: `user_version` was a watermark of what ran.
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]
        done = {n for n in files if n <= current}
        conn.executemany("INSERT INTO migrations VALUES (?)", [(n,) for n in done])
    applied: list[int] = []
    for number in sorted(set(files) - done):
        # executescript commits any open transaction first, so the BEGIN lives in the script.
        script = (
            f"BEGIN;\n{files[number].read_text()}\n"
            f"INSERT INTO migrations VALUES ({number});\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(number)
    return applied
