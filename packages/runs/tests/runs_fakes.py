"""Fakes and helpers shared by the runs tests."""

import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_runs import MIGRATIONS, Run, StageDef


def migrated(root: Path) -> Path:
    db = root / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return db


class FakeConnector:
    """Records every call; raises for a stage named `explode`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run: Run, stage_id: str, stage: StageDef) -> None:
        self.calls.append((stage_id, stage.name))
        if stage.name == "explode":
            raise RuntimeError("connector said no")


def wait_for(check: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)
