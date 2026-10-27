from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_learning import MIGRATIONS, Learning, SqliteStorage


@pytest.fixture
def learning(tmp_path: Path) -> Learning:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    (tmp_path / "worlds" / "world_1").mkdir(parents=True)
    return Learning(SqliteStorage(db), tmp_path)
