"""A migrated swarmify database for the swarm tests."""

from contextlib import closing
from pathlib import Path

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

    def stage_folder(self, world_id: str, run_id: str, stage_id: str) -> Path:
        folder = self.root / world_id / "runs" / run_id / stage_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def artifacts(self, world_id: str, run_id: str) -> list[str]:
        return []

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
