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
