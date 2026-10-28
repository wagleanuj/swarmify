import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

from swarmify_connectors import MIGRATIONS, Connectors, Credentials, FakeTransport, SqliteStorage
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return db


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def connectors(tmp_path: Path, db: Path, transport: FakeTransport) -> Connectors:
    return Connectors(SqliteStorage(db), transport, Credentials(tmp_path / ".env"), poll_seconds=0)


@pytest.fixture
def fake_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake Remotion CLI first on PATH, as `npx`, and a fake `npm` that prints its command
    and makes `node_modules`. Returns a ready Remotion project, its dependencies installed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_npx.py").read_text())
    npm = bin_dir / "npm"
    npm.write_text(
        f"#!{sys.executable}\nimport os, sys\nprint('fake npm', *sys.argv[1:])\n"
        "os.mkdir('node_modules')\n"
    )
    for tool in (npx, npm):
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    project = tmp_path / "video"
    (project / "src").mkdir(parents=True)
    (project / "node_modules").mkdir()
    return project
