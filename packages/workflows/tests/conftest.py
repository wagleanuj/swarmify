from pathlib import Path

import pytest

from samples import CONNECTORS, migrated_db
from swarmify_workflows import SqliteStorage, Workflows


@pytest.fixture
def workflows(tmp_path: Path) -> Workflows:
    return Workflows(SqliteStorage(migrated_db(tmp_path)), lambda world_id: CONNECTORS)
