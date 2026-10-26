"""Memory context: Notes and Artifacts are markdown files with frontmatter in the run folder
(files are memory, ADR 0001). Every write appends an `artifact.created` event (ArtifactCreated
in the Context Map); a note is an artifact of type `note`.

Layout: `<data_dir>/worlds/<world>/runs/<run>/<stage>/notes/<slug>.md` and
`.../<stage>/artifacts/<name>`. An artifact that is not markdown gets a sidecar `<name>.md`.
"""

import json
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect

__all__ = ["Memory"]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


def _frontmatter(fields: dict[str, str | None]) -> str:
    """JSON scalars are valid YAML, so values never need hand escaping."""
    lines = [f"{k}: {json.dumps(v)}" for k, v in fields.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


class Memory:
    def __init__(self, db: Path, data_dir: Path) -> None:
        self.db = db
        self.data_dir = data_dir

    def world_folder(self, world_id: str) -> Path:
        return self.data_dir / "worlds" / world_id

    def stage_folder(self, world_id: str, run_id: str, stage_id: str) -> Path:
        folder = self.world_folder(world_id) / "runs" / run_id / stage_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def artifacts(self, world_id: str, run_id: str) -> list[str]:
        run = self.world_folder(world_id) / "runs" / run_id
        files = [p for p in run.glob("*/artifacts/*") if p.is_file()]
        sidecars = {p.with_name(f"{p.name}.md") for p in files}
        return sorted(str(p.relative_to(run)) for p in files if p not in sidecars)

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
        title = title.strip()
        if not title:
            raise ValueError("a note needs a title")
        folder = self.stage_folder(world_id, run_id, stage_id) / "notes"
        folder.mkdir(exist_ok=True)
        path, n = folder / f"{_slug(title)}.md", 1
        while path.exists():
            n += 1
            path = folder / f"{_slug(title)}-{n}.md"
        meta = self._meta("note", author, run_id, stage_id, task_id)
        path.write_text(f"{_frontmatter(meta)}# {title}\n\n{body.strip()}\n")
        return self._created(world_id, run_id, stage_id, author, "note", path)

    def write_artifact(
        self, world_id: str, run_id: str, stage_id: str, author: str, name: str, content: str
    ) -> str:
        """Writing a name again replaces the file."""
        if not _NAME.match(name):
            raise ValueError(f"{name!r} is not a plain file name like ideas.md")
        folder = self.stage_folder(world_id, run_id, stage_id) / "artifacts"
        folder.mkdir(exist_ok=True)
        path = folder / name
        meta = _frontmatter(self._meta("file", author, run_id, stage_id, None))
        if path.suffix == ".md":
            path.write_text(meta + content)
        else:
            path.write_text(content)
            (folder / f"{name}.md").write_text(f"{meta}Sidecar of {name}.\n")
        return self._created(world_id, run_id, stage_id, author, "file", path)

    @staticmethod
    def _meta(
        type: str, author: str, run_id: str, stage_id: str, task_id: str | None
    ) -> dict[str, str | None]:
        return {
            "type": type,
            "author": author,
            "run": run_id,
            "stage": stage_id,
            "task": task_id,
            "created": datetime.now(UTC).isoformat(),
        }

    def _created(
        self, world_id: str, run_id: str, stage_id: str, author: str, type: str, path: Path
    ) -> str:
        rel = str(path.relative_to(self.world_folder(world_id)))
        event = Event.new(
            "artifact.created",
            world_id=world_id,
            run_id=run_id,
            payload={"type": type, "path": rel, "author": author, "stage_id": stage_id},
        )
        with closing(connect(self.db)) as conn:
            append(conn, event)
        return rel
