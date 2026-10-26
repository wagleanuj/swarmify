import sqlite3
from pathlib import Path

import pytest

from swarmify_kernel.sqlite import connect, migrate


def _write(dir: Path, name: str, sql: str) -> None:
    dir.mkdir(exist_ok=True)
    (dir / name).write_text(sql)


def test_migrate_applies_numbered_files_in_order_and_records_user_version(tmp_path: Path) -> None:
    _write(tmp_path / "m", "0002_b.sql", "INSERT INTO t VALUES ('b');")
    _write(tmp_path / "m", "0001_a.sql", "CREATE TABLE t (v TEXT);")
    conn = connect(tmp_path / "x.db")
    assert migrate(conn, tmp_path / "m") == [1, 2]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [r["v"] for r in conn.execute("SELECT v FROM t")] == ["b"]


def test_migrate_is_idempotent_and_picks_up_new_files(tmp_path: Path) -> None:
    _write(tmp_path / "m", "0001_a.sql", "CREATE TABLE t (v TEXT);")
    conn = connect(tmp_path / "x.db")
    migrate(conn, tmp_path / "m")
    assert migrate(conn, tmp_path / "m") == []
    _write(tmp_path / "m", "0002_b.sql", "INSERT INTO t VALUES ('b');")
    assert migrate(conn, tmp_path / "m") == [2]


def test_migrate_merges_directories_and_rejects_duplicate_numbers(tmp_path: Path) -> None:
    _write(tmp_path / "a", "0001_a.sql", "CREATE TABLE a (v TEXT);")
    _write(tmp_path / "b", "0002_b.sql", "CREATE TABLE b (v TEXT);")
    conn = connect(tmp_path / "x.db")
    assert migrate(conn, tmp_path / "a", tmp_path / "b") == [1, 2]
    _write(tmp_path / "c", "0002_c.sql", "CREATE TABLE c (v TEXT);")
    with pytest.raises(ValueError, match="0002"):
        migrate(conn, tmp_path / "b", tmp_path / "c")


def test_failed_migration_rolls_back_and_leaves_version(tmp_path: Path) -> None:
    _write(tmp_path / "m", "0001_a.sql", "CREATE TABLE t (v TEXT); INSERT INTO nope VALUES (1);")
    conn = connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, tmp_path / "m")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t'").fetchone() is None


def test_connect_sets_wal_foreign_keys_and_row_factory(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    assert (tmp_path / "x.db").exists()
