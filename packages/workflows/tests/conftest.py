from contextlib import closing
from pathlib import Path

import pytest

from samples import CONNECTORS
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_workflows import MIGRATIONS, SqliteStorage, Workflows


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return db


@pytest.fixture
def workflows(tmp_path: Path) -> Workflows:
    return Workflows(SqliteStorage(migrated_db(tmp_path)), lambda world_id: CONNECTORS)
