"""Memory context: Notes and Artifacts are markdown files with frontmatter in the run folder
(files are memory, ADR 0001). Every write appends an `artifact.created` event (ArtifactCreated
in the Context Map); a note is an artifact of type `note`.

Layout: `<data_dir>/worlds/<world>/runs/<run>/<stage>/notes/<slug>.md` and
`.../<stage>/artifacts/<name>`. An artifact that is not markdown gets a sidecar `<name>.md`.
Typed artifacts the judge projects (idea, verdict, ...) carry their type and fields in the
frontmatter.

The Library is every artifact of a world across its runs, read from the files; a Pin marks one
as required reading for later swarm stages and lives in its frontmatter, so it survives any
database. The world Graph is built from those files by the GraphWriter alone (ADR 0003) and read
through the GraphIndex port.
"""

import json
import re
import shutil
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_memory.fake import FakeGraphIndex
from swarmify_memory.graph import McpGraphIndex
from swarmify_memory.ports import EMPTY, GRAPH_OUT, GRAPH_TOOLS, GodNode, GraphIndex, graph_size
from swarmify_memory.writer import DEBOUNCE, GraphWriter, extract_command, run_graphify

__all__ = [
    "DEBOUNCE",
    "EMPTY",
    "ArtifactFile",
    "GRAPH_OUT",
    "GRAPH_TOOLS",
    "FakeGraphIndex",
    "GodNode",
    "GraphIndex",
    "GraphWriter",
    "McpGraphIndex",
    "Memory",
    "extract_command",
    "graph_size",
    "run_graphify",
]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


def _frontmatter(fields: dict[str, Any]) -> str:
    """JSON values are valid YAML (flow style), so values never need hand escaping."""
    lines = [f"{k}: {json.dumps(v)}" for k, v in fields.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _split(text: str) -> tuple[dict[str, Any], str]:
    """Frontmatter as written by `_frontmatter`, and the body after it. A value that is not
    JSON (a hand edit) is kept as its text."""
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        return {}, text
    head, body = text[4:].split("\n---\n", 1)
    fields: dict[str, Any] = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            try:
                fields[key] = json.loads(value)
            except ValueError:
                fields[key] = value
    return fields, body.lstrip("\n")


@dataclass(frozen=True)
class ArtifactFile:
    """An artifact of a stage as its file says: `fields` is the whole frontmatter."""

    name: str
    path: str
    """Relative to the world folder."""
    type: str
    author: str
    title: str
    fields: dict[str, Any]
    body: str
    run_id: str = ""
    stage: str = ""
    """The stage folder: a stage id for what agents wrote, `<nn>-<name>` for a gate decision."""
    created: str = ""
    pinned: bool = False


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
        files = [p for p in run.glob("*/artifacts/[!.]*") if p.is_file()]
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
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
        name: str,
        content: str,
        type: str = "file",
        fields: dict[str, Any] | None = None,
    ) -> str:
        """Writing a name again replaces the file. `fields` join the frontmatter."""
        if not _NAME.match(name):
            raise ValueError(f"{name!r} is not a plain file name like ideas.md")
        folder = self.stage_folder(world_id, run_id, stage_id) / "artifacts"
        folder.mkdir(exist_ok=True)
        path = folder / name
        meta = _frontmatter({**self._meta(type, author, run_id, stage_id, None), **(fields or {})})
        if path.suffix == ".md":
            path.write_text(meta + content)
        else:
            path.write_text(content)
            (folder / f"{name}.md").write_text(f"{meta}Sidecar of {name}.\n")
        return self._created(world_id, run_id, stage_id, author, type, path)

    def record(self, world_id: str, run_id: str, stage_id: str, author: str, path: Path) -> str:
        """Announce an artifact a connector wrote itself, typed by its sidecar when it has one."""
        sidecar = path.with_name(f"{path.name}.md")
        fields = _split(sidecar.read_text())[0] if sidecar.is_file() else {}
        return self._created(world_id, run_id, stage_id, author, fields.get("type", "file"), path)

    def copy_artifacts(
        self,
        source: tuple[str, str],
        names: list[str],
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
    ) -> list[str]:
        """Copy the named artifacts of run `source` (world id, run id) into a stage, sidecars
        included: how a workflow stage takes its child run's artifacts. Each file keeps its
        frontmatter, which names the run that made it. When several stages of the source made
        one name, the latest wins. Raises ValueError, copying nothing, when a name is missing.
        """
        run = self.world_folder(source[0]) / "runs" / source[1]
        found: dict[str, Path] = {}
        for name in names:
            matches = list(run.glob(f"*/artifacts/{name}")) if _NAME.match(name) else []
            if not matches:
                raise ValueError(f"the child run made no artifact named {name}")
            found[name] = max(matches, key=lambda p: p.stat().st_mtime)
        folder = self.stage_folder(world_id, run_id, stage_id) / "artifacts"
        folder.mkdir(exist_ok=True)
        out = []
        for name, path in found.items():
            shutil.copyfile(path, folder / name)
            sidecar = path.with_name(f"{name}.md")
            if path.suffix != ".md" and sidecar.exists():
                shutil.copyfile(sidecar, folder / sidecar.name)
            out.append(self._created(world_id, run_id, stage_id, author, "file", folder / name))
        return out

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
        """A typed artifact (idea, verdict, ...) as markdown: `type` and `fields` go in the
        frontmatter, `fields` never overriding the reserved keys (type, author, run, ...) since
        they may come from model output. Writing a name again replaces the file."""
        if not _NAME.match(name) or not name.endswith(".md"):
            raise ValueError(f"{name!r} is not a plain markdown file name like idea-hooks.md")
        folder = self.stage_folder(world_id, run_id, stage_id) / "artifacts"
        folder.mkdir(exist_ok=True)
        path = folder / name
        meta: dict[str, Any] = self._meta(type, author, run_id, stage_id, None)
        meta |= {k: v for k, v in fields.items() if k not in meta}
        path.write_text(_frontmatter(meta) + body)
        return self._created(world_id, run_id, stage_id, author, type, path)

    def notes(self, world_id: str, run_id: str, stage_id: str) -> list[tuple[str, str]]:
        """The stage's notes by name: (path relative to the world folder, body)."""
        folder = self.world_folder(world_id) / "runs" / run_id / stage_id / "notes"
        world = self.world_folder(world_id)
        return [
            (str(p.relative_to(world)), _split(p.read_text())[1])
            for p in sorted(folder.glob("*.md"))
        ]

    def stage_artifacts(self, world_id: str, run_id: str, stage_id: str) -> list[ArtifactFile]:
        """The stage's artifacts by name, sidecars standing in for their files."""
        folder = self.world_folder(world_id) / "runs" / run_id / stage_id / "artifacts"
        files = [p for p in folder.glob("[!.]*") if p.is_file()]  # dotfiles: connector state
        return self._artifacts(world_id, files)

    def library(self, world_id: str) -> list[ArtifactFile]:
        """The Library: every artifact of the world across its runs, newest first, gate
        decisions included. A binary's sidecar stands in for it."""
        # ponytail: a walk over the world's files per call; index them in a table when a world
        # holds tens of thousands of artifacts.
        world = self.world_folder(world_id)
        files = [p for p in world.glob("runs/*/*/artifacts/[!.]*") if p.is_file()]
        files += [p for p in world.glob("runs/*/*/decision*.md") if p.is_file()]
        return sorted(self._artifacts(world_id, files), key=lambda a: a.created, reverse=True)

    def pinned(self, world_id: str) -> list[str]:
        """The pinned artifacts, as paths relative to the world folder, newest first."""
        return [a.path for a in self.library(world_id) if a.pinned]

    def pin(self, world_id: str, path: str, pinned: bool = True) -> ArtifactFile:
        """Pin the artifact at `path` (relative to the world folder) as required reading for
        later swarm stages, or unpin it. The flag lives in the markdown's frontmatter, a
        binary's in its sidecar. Raises ValueError when there is no such artifact."""
        world = self.world_folder(world_id)
        runs = (world / "runs").resolve()
        file = (world / path).resolve()
        if file.suffix != ".md" and file.with_name(f"{file.name}.md").is_file():
            file = file.with_name(f"{file.name}.md")
        if not (file.is_relative_to(runs) and file.suffix == ".md" and file.is_file()):
            raise ValueError(f"no artifact at {path}")
        file = world / file.relative_to(runs.parent)  # resolved only for the check above
        fields, body = _split(file.read_text(errors="replace"))
        if pinned:
            fields["pinned"] = True
        else:
            fields.pop("pinned", None)
        file.write_text(_frontmatter(fields) + body)
        (artifact,) = self._artifacts(world_id, [file])
        event = Event.new(
            "artifact.pinned",
            world_id=world_id,
            run_id=artifact.run_id or None,
            payload={"type": artifact.type, "path": artifact.path, "pinned": pinned},
        )
        with closing(connect(self.db)) as conn:
            append(conn, event)
        return artifact

    def _artifacts(self, world_id: str, files: list[Path]) -> list[ArtifactFile]:
        """The files as artifacts, by path; a binary with a sidecar is read through it."""
        world = self.world_folder(world_id)
        out = []
        for p in sorted(files):
            if p.suffix != ".md" and p.with_name(f"{p.name}.md").is_file():
                continue  # its sidecar carries it
            fields, body = _split(p.read_text(errors="replace"))
            carried = p.suffix == ".md" and p.with_name(p.name[:-3]).is_file()
            parts = p.relative_to(world).parts  # runs, <run>, <stage>, ...
            out.append(
                ArtifactFile(
                    p.name[:-3] if carried else p.name,
                    str(p.relative_to(world)),
                    str(fields.get("type", "file")),
                    str(fields.get("author", "")),
                    str(fields.get("title") or p.name),
                    fields,
                    body,
                    parts[1] if len(parts) > 2 else "",
                    parts[2] if len(parts) > 2 else "",
                    str(fields.get("created") or ""),
                    fields.get("pinned") is True,
                )
            )
        return out

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
