"""A migrated swarmify database for the swarm tests."""

from contextlib import closing
from pathlib import Path
from typing import Any

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_swarm import MIGRATIONS


def migrated(root: Path) -> Path:
    db = root / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return db


class DiskFiles:
    """Files for the worker tests: notes and artifacts as plain files under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.typed: list[tuple[str, str, dict[str, Any], str]] = []
        """Every typed artifact written: (name, type, fields, body)."""

    def stage_folder(self, world_id: str, run_id: str, stage_id: str) -> Path:
        folder = self.root / world_id / "runs" / run_id / stage_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def artifacts(self, world_id: str, run_id: str) -> list[str]:
        run = self.root / world_id / "runs" / run_id
        return sorted(str(p.relative_to(run)) for p in run.glob("*/artifacts/*") if p.is_file())

    def write_note(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
        title: str,
        body: str,
        task_id: str | None = None,
    ) -> str:
        path = self.stage_folder(world_id, run_id, stage_id) / "notes" / f"{title}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"{author} {task_id}\n{body}")
        return str(path.relative_to(self.root / world_id))

    def write_artifact(
        self, world_id: str, run_id: str, stage_id: str, author: str, name: str, content: str
    ) -> str:
        if "/" in name:
            raise ValueError("not a plain file name")
        path = self.stage_folder(world_id, run_id, stage_id) / "artifacts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
        return str(path.relative_to(self.root / world_id))

    def notes(self, world_id: str, run_id: str, stage_id: str) -> list[tuple[str, str]]:
        folder = self.stage_folder(world_id, run_id, stage_id) / "notes"
        return [
            (str(p.relative_to(self.root / world_id)), p.read_text())
            for p in sorted(folder.glob("*.md"))
        ]

    def write_typed(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
        name: str,
        type: str,
        fields: dict[str, Any],
        body: str,
    ) -> str:
        self.typed.append((name, type, fields, body))
        return self.write_artifact(world_id, run_id, stage_id, author, name, body)


class DiskGraphs:
    """Graphs for the worker tests: a world is any folder under `root`; calls are recorded by
    world folder name and answered with a fixed line."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def folder(self, world: str) -> Path | None:
        folder = self.root / world
        return folder if folder.is_dir() else None

    def call(self, folder: Path, tool: str, args: dict[str, object]) -> str:
        self.calls.append((folder.name, tool, args))
        return "Hooks work."
