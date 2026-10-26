from pathlib import Path

import pytest

from swarm_db import migrated
from swarmify_swarm import SqliteStorage


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return migrated(tmp_path)


@pytest.fixture
def board(db: Path) -> SqliteStorage:
    return SqliteStorage(db)
