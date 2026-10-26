from collections.abc import Iterator
from pathlib import Path

import pytest

from runs_fakes import FakeConnector, migrated
from swarmify_runs import Runner, SqliteStorage


@pytest.fixture
def storage(tmp_path: Path) -> SqliteStorage:
    return SqliteStorage(migrated(tmp_path))


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector()


@pytest.fixture
def runner(tmp_path: Path, storage: SqliteStorage, connector: FakeConnector) -> Iterator[Runner]:
    """Real DBOS on SQLite in a temp folder."""
    runner = Runner(storage, tmp_path / "dbos.sqlite", connector, poll_seconds=0.05)
    runner.launch()
    yield runner
    runner.shutdown()
