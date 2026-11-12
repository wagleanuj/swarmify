"""Memory context: Notes and Artifacts are markdown files with frontmatter in the run folder
(files are memory, ADR 0001). Every write appends an `artifact.created` event (ArtifactCreated
in the Context Map); a note is an artifact of type `note`.

Layout: `<data_dir>/worlds/<world>/runs/<run>/<stage>/notes/<slug>.md` and
`.../<stage>/artifacts/<name>`. An artifact that is not markdown gets a sidecar `<name>.md`.
Typed artifacts the judge projects (idea, verdict, ...) carry their type and fields in the
frontmatter.

The Library is every artifact of a world across its runs, read from the files; a Pin marks one
as required reading for later swarm stages and lives in its frontmatter, so it survives any
database. Retention (`prune`) deletes a world's old run binaries but never markdown: each deleted
file's sidecar stays, saying when it went, so the graph and the library keep the record (ADR
0001). Outcomes (`record_outcome`) change every few hours, so they live in a hidden sidecar
beside their publish artifact (OUTCOMES), never in it: the artifact is what the graph extracts,
and rewriting it re-extracted it on every view count. Readers get them merged into its fields.

Assets are the operator's reusable files (music beds, sound effects, logos) in
`<data_dir>/worlds/<world>/assets/`: added over HTTP or by hand, read by connector stages and the
build session, listed to swarm agents, never written by an agent and never part of the graph.

The world Graph is built from those files by the GraphWriter alone (ADR 0003) and read through
the GraphIndex port.
"""

import json
import os
import re
import shutil
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_memory.fake import FakeGraphIndex
from swarmify_memory.graph import McpGraphIndex
from swarmify_memory.ports import (
    ASSETS,
    EMPTY,
    GRAPH_OUT,
    GRAPH_TOOLS,
    OUTCOMES,
    Community,
    GodNode,
    GraphIndex,
    GraphNode,
    Surprise,
    graph_map,
    graph_size,
)
from swarmify_memory.writer import (
    DEBOUNCE,
    GraphWriter,
    ShrinkRefused,
    extract_command,
    run_graphify,
)

__all__ = [
    "ASSETS",
    "DEBOUNCE",
    "EMPTY",
    "ArtifactFile",
    "GRAPH_OUT",
    "GRAPH_TOOLS",
    "OUTCOMES",
    "FakeGraphIndex",
    "Community",
    "GodNode",
    "GraphNode",
    "Surprise",
    "GraphIndex",
    "GraphWriter",
    "McpGraphIndex",
    "Memory",
    "ShrinkRefused",
    "extract_command",
    "graph_map",
    "graph_size",
    "library",
    "made",
    "run_graphify",
]

OUTCOME = "## Outcome\n\n"
"""The body section records from before the sidecar carry their outcome in."""
OUTCOME_FIELDS = ("outcomes", "outcomes_at", "primary_metric", "views", "views_at")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
INLINE_MAX = 1 << 20
"""The largest file an artifact listing inlines as text, in bytes."""


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


def _frontmatter(fields: dict[str, Any]) -> str:
    """JSON values are valid YAML (flow style), so values never need hand escaping."""
    lines = [f"{k}: {json.dumps(v)}" for k, v in fields.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _own_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """The YAML frontmatter a writer put at the top of its own markdown, and the body after it;
    ({}, content) when there is none or it does not parse to a mapping."""
    if not content.startswith("---\n") or "\n---\n" not in content[3:]:
        return {}, content
    head, body = content[4:].split("\n---\n", 1)
    try:
        fields = yaml.safe_load(head)
    except yaml.YAMLError:
        return {}, content
    return (fields, body.lstrip("\n")) if isinstance(fields, dict) else ({}, content)


def _text(file: Path) -> str | None:
    """The file's text, or None for one over INLINE_MAX or a binary: one with a NUL byte in its
    first 8 KB, the test git uses. A stray bad byte in text is replaced, not a reason to skip."""
    if file.stat().st_size > INLINE_MAX:
        return None
    with file.open("rb") as f:
        if b"\0" in f.read(8192):
            return None
    return file.read_text(errors="replace")


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


def _outcomes_of(file: Path) -> Path:
    return file.with_name(f".{file.name}{OUTCOMES}")


def _with_outcomes(file: Path, fields: dict[str, Any]) -> dict[str, Any]:
    """The artifact's fields with its sidecar's outcomes, which win over any in the file."""
    try:
        found = json.loads(_outcomes_of(file).read_text())
    except (OSError, ValueError):
        return fields
    if not isinstance(found, dict):
        return fields
    return {k: v for k, v in fields.items() if k not in ("views", "views_at")} | found


TEMPLATE_RECORD = ".template.json"
"""What marks a Template's folder in the assets (the connectors' build_session.RECORD)."""


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
    """The stage folder, `stage_<id>`, for what agents wrote and for a gate decision alike."""
    created: str = ""
    pinned: bool = False
    size: int = 0
    """Bytes on disk of the file it stands for (a sidecar's binary). `body` is empty for a
    binary or a file over INLINE_MAX: its bytes are served by the world's files route."""


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
        kind: str = "file",
        fields: dict[str, Any] | None = None,
    ) -> str:
        """Writing a name again replaces the file. `kind` is the artifact type; `fields` join
        the frontmatter."""
        if not _NAME.match(name):
            raise ValueError(f"{name!r} is not a plain file name like ideas.md")
        folder = self.stage_folder(world_id, run_id, stage_id) / "artifacts"
        folder.mkdir(exist_ok=True)
        path = folder / name
        core = {**self._meta(kind, author, run_id, stage_id, None), **(fields or {})}
        meta = _frontmatter(core)
        if path.suffix == ".md":
            # A writer's own frontmatter joins ours (ours wins on clashes) rather than stacking
            # under it, where readers of the first block, like a publish stage, never saw it.
            own, content = _own_frontmatter(content)
            meta = _frontmatter(core | {k: v for k, v in own.items() if k not in core})
            path.write_text(meta + content)
        else:
            path.write_text(content)
            (folder / f"{name}.md").write_text(f"{meta}Sidecar of {name}.\n")
        return self._created(world_id, run_id, stage_id, author, kind, path)

    def record(self, world_id: str, run_id: str, stage_id: str, author: str, path: Path) -> str:
        """Announce an artifact a connector wrote itself, typed by its sidecar when it has one."""
        sidecar = path.with_name(f"{path.name}.md")
        fields = _split(sidecar.read_text())[0] if sidecar.is_file() else {}
        return self._created(world_id, run_id, stage_id, author, fields.get("type", "file"), path)

    def copy_artifacts(
        self,
        source: tuple[str, str, list[str]],
        names: list[str],
        world_id: str,
        run_id: str,
        stage_id: str,
        author: str,
    ) -> list[str]:
        """Copy the named artifacts of run `source` (world id, run id, stage ids in run order)
        into a stage, sidecars included: how a workflow stage takes its child run's artifacts.
        Each file keeps its frontmatter, which names the run that made it. When several stages
        of the source made one name, the latest stage wins. Raises ValueError, copying nothing,
        when a name is missing.
        """
        run = self.world_folder(source[0]) / "runs" / source[1]
        found: dict[str, Path] = {}
        for name in names:
            matches = [run / s / "artifacts" / name for s in reversed(source[2])]
            path = next((p for p in matches if p.is_file()), None) if _NAME.match(name) else None
            if path is None:
                raise ValueError(f"the child run made no artifact named {name}")
            found[name] = path
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

    def decisions(self, world_id: str, run_id: str, stage_id: str) -> list[ArtifactFile]:
        """The gate decisions written in the stage's folder, oldest first."""
        folder = self.world_folder(world_id) / "runs" / run_id / stage_id
        return self._artifacts(world_id, [p for p in folder.glob("decision*.md") if p.is_file()])

    def file(self, world_id: str, path: str) -> Path:
        """The run file or asset at `path` (relative to the world folder), for serving it as it
        is. Raises ValueError when there is no such file under the world's runs or assets."""
        file = self._inside(world_id, path)
        world = self.world_folder(world_id).resolve()
        if not any(file.is_relative_to(world / f) for f in ("runs", ASSETS)):
            raise ValueError(f"{path} is not a run file or asset of this world")
        return file

    def assets(self, world_id: str) -> list[str]:
        """The world's assets, subfolders included, as paths relative to its folder. A Template
        (a folder with its `.template.json` record) is not walked: it is listed as a Template,
        and its node_modules alone would be thousands of paths."""
        world = self.world_folder(world_id)
        files: list[Path] = []
        for root, dirs, names in os.walk(world / ASSETS):
            if TEMPLATE_RECORD in names:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            files += [Path(root) / n for n in names if not n.startswith(".")]
        return sorted(str(p.relative_to(world)) for p in files)

    def add_asset(self, world_id: str, name: str, data: bytes | Path, folder: str = "") -> str:
        """Write (or replace) the asset `name`, a plain file name, in the assets folder or its
        subfolder `folder`, a plain name too, from `data` or by moving the file `data` (on the
        same disk), as an upload streamed there. Returns its path relative to the world folder.
        Raises ValueError for a name that is a path or a dotfile."""
        for plain in (name, folder) if folder else (name,):
            if not _NAME.match(plain):
                raise ValueError(f"an asset needs a plain file name, got '{plain}'")
        path = f"{ASSETS}/{folder}/{name}" if folder else f"{ASSETS}/{name}"
        file = self.world_folder(world_id) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, Path):
            data.replace(file)
        else:
            file.write_bytes(data)
        return path

    def remove_asset(self, world_id: str, path: str) -> None:
        """Delete the asset at `path` (relative to the world folder). Raises ValueError when it
        is not a file under the world's assets."""
        self._asset(world_id, path).unlink()

    def rename_asset(self, world_id: str, path: str, name: str) -> str:
        """Rename the asset at `path` to `name`, a plain file name, in the same folder. Returns
        its new path. Raises ValueError for a bad name, a name taken, or no such asset."""
        if not _NAME.match(name):
            raise ValueError(f"an asset needs a plain file name, got '{name}'")
        file = self._asset(world_id, path)
        target = file.with_name(name)
        if target.exists():
            raise ValueError(f"{name} is already there")
        file.rename(target)
        return str(target.relative_to(self.world_folder(world_id).resolve()))

    def _asset(self, world_id: str, path: str) -> Path:
        file = self._inside(world_id, path)
        if not file.is_relative_to(self.world_folder(world_id).resolve() / ASSETS):
            raise ValueError(f"{path} is not an asset of this world")
        return file

    def library(self, world_id: str) -> list[ArtifactFile]:
        """The Library: every artifact of the world across its runs, newest first, gate
        decisions included. A binary's sidecar stands in for it."""
        return library(self.world_folder(world_id))

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
        return _read(self.world_folder(world_id), files)

    def made(self, world_id: str, runs: Mapping[str, str]) -> list[str]:
        """See `made`."""
        return made(self.world_folder(world_id), runs)

    def publish_records(self, world_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Every publish artifact of the world: (path relative to the world folder, fields)."""
        world = self.world_folder(world_id)
        return [
            (str(p.relative_to(world)), _with_outcomes(p, _split(p.read_text())[0]))
            for p in sorted(world.glob("runs/*/*/artifacts/publish.md"))
        ]

    def record_outcome(
        self,
        world_id: str,
        path: str,
        metrics: dict[str, float],
        primary: str,
        at: datetime,
        visibility: Mapping[str, Any] | None = None,
    ) -> None:
        """Write a publish artifact's Outcome (`outcomes` by metric, `outcomes_at` and
        `primary_metric`, the one the scorecard ranks by) into its sidecar, replacing the last,
        with `visibility`, how its platform shows the post (`visible`, `visibility_label`), when
        one says.
        A record from before the sidecar has its outcome fields and Outcome section taken out,
        once; after that the record itself is never written for an outcome."""
        file = self._inside(world_id, path)
        fields, body = _split(file.read_text())
        if any(k in fields for k in OUTCOME_FIELDS) or OUTCOME in body:
            kept = {k: v for k, v in fields.items() if k not in OUTCOME_FIELDS}
            file.write_text(_frontmatter(kept) + body.split(OUTCOME)[0].rstrip("\n") + "\n")
        found = {
            "outcomes": metrics,
            "outcomes_at": at.astimezone(UTC).isoformat(),
            "primary_metric": primary,
            **(visibility or {}),
        }
        # whole or not at all, so a scorecard read meanwhile never sees half a sidecar; the temp
        # name ends like a sidecar, so the graph's watch and prune pass over it too
        temp = file.with_name(f".{file.name}.writing{OUTCOMES}")
        temp.write_text(json.dumps(found))
        temp.replace(_outcomes_of(file))
        self._event(
            world_id,
            fields.get("run"),
            "outcome.recorded",
            path=path,
            metrics=metrics,
            primary=primary,
        )

    def prune(self, world_id: str, before: datetime, now: datetime) -> list[str]:
        """Delete every run file of the world that is not markdown and was last changed before
        `before`, keeping its sidecar (written when there was none) with `deleted` (`now`) in the
        frontmatter and a line in the body. Returns the deleted paths."""
        world = self.world_folder(world_id)
        cutoff, gone = before.timestamp(), []
        when = before.astimezone(UTC).isoformat()
        for file in sorted((world / "runs").rglob("*")):
            if file.suffix == ".md" or file.name.endswith(OUTCOMES) or not file.is_file():
                continue
            if file.stat().st_mtime >= cutoff:
                continue
            sidecar = file.with_name(f"{file.name}.md")
            fields, body = _split(sidecar.read_text()) if sidecar.exists() else ({}, "")
            run = file.relative_to(world / "runs").parts[0]
            fields = {
                "type": "file",
                "run": run,
                **fields,
                "deleted": now.astimezone(UTC).isoformat(),
            }
            body = body.rstrip("\n") or f"Sidecar of {file.name}."
            sidecar.write_text(
                f"{_frontmatter(fields)}{body}\n\n"
                f"{file.name} was deleted by retention: unchanged since before {when}."
                " This record stays.\n"
            )
            file.unlink()
            rel = str(file.relative_to(world))
            gone.append(rel)
            self._event(world_id, run, "artifact.pruned", path=rel)
        return gone

    def _inside(self, world_id: str, path: str) -> Path:
        world = self.world_folder(world_id).resolve()
        file = (world / path).resolve()
        if not file.is_relative_to(world) or not file.is_file():
            raise ValueError(f"{path} is not a file of this world")
        return file

    def _event(self, world_id: str, run_id: Any, type: str, **payload: Any) -> None:
        event = Event.new(
            type, world_id=world_id, run_id=str(run_id) if run_id else None, payload=payload
        )
        with closing(connect(self.db)) as conn:
            append(conn, event)

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


def _read(world: Path, files: list[Path]) -> list[ArtifactFile]:
    """The files as artifacts, by path; a binary with a sidecar is read through it, one
    without is listed by name and size only, as is a file over INLINE_MAX."""
    out = []
    for p in sorted(files):
        if p.suffix != ".md" and p.with_name(f"{p.name}.md").is_file():
            continue  # its sidecar carries it
        text = _text(p)
        fields, body = _split(text) if text is not None else ({}, "")
        fields = _with_outcomes(p, fields)
        binary = p.with_name(p.name[:-3])
        carried = p.suffix == ".md" and (binary.is_file() or "deleted" in fields)
        if carried:  # retention may have deleted the binary and kept its sidecar
            size = binary.stat().st_size if binary.is_file() else 0
        else:
            size = p.stat().st_size
        parts = p.relative_to(world).parts  # runs, <run>, <stage>, ...
        out.append(
            ArtifactFile(
                p.name[:-3] if carried else p.name,
                str(p.relative_to(world)),
                str(fields.get("type", "file")),
                str(fields.get("author", "")),
                str(fields.get("title") or (p.name[:-3] if carried else p.name)),
                fields,
                body,
                parts[1] if len(parts) > 2 else "",
                parts[2] if len(parts) > 2 else "",
                str(fields.get("created") or ""),
                fields.get("pinned") is True,
                size,
            )
        )
    return out


def library(world: Path) -> list[ArtifactFile]:
    """The Library of the world at `world` (Memory.library)."""
    # ponytail: a walk over the world's files per call; index them in a table when a world
    # holds tens of thousands of artifacts.
    files = [p for p in world.glob("runs/*/*/artifacts/[!.]*") if p.is_file()]
    files += [p for p in world.glob("runs/*/*/decision*.md") if p.is_file()]
    return sorted(_read(world, files), key=lambda a: a.created, reverse=True)


MADE_MAX = 100
"""The most runs `made` lists, newest first."""
GIST = 400
"""The most characters of a made run's gist."""
_NOT_GIST = {
    "type", "author", "run", "stage", "task", "created", "title", "rank", "reason", "sources",
    "pinned", "connector", "status", "id", "links", "sent", *OUTCOME_FIELDS,
}  # fmt: skip


def _bare(title: str) -> str:
    """A title without the list number a pick may carry ("#4 X", "Idea 2: X")."""
    return re.sub(r"^\s*(?:idea\s*#?\s*\d+|#\d+)\s*[:.)-]?\s+", "", title, flags=re.I).strip()


def retention(outcomes: Any) -> list[str]:
    """An Outcome's retention metrics in words: a `_percent` one as "avg viewed 62.5%", a
    `_seconds` one as "avg view 12.0 s"."""
    out = []
    for metric, value in (outcomes if isinstance(outcomes, dict) else {}).items():
        if not isinstance(value, int | float):
            continue
        for suffix, unit in (("_percent", "%"), ("_seconds", " s")):
            if metric.endswith(suffix):
                out.append(f"{metric.removesuffix(suffix).replace('_', ' ')} {value:.1f}{unit}")
    return out


def made(world: Path, runs: Mapping[str, str]) -> list[str]:
    """What the world at `world` already made, one line per run, newest first: every run with a
    publish record, and every run in `runs` (run id to status: the runs the caller counts, not
    failed or aborted ones) that settled on one idea. A run's subject is what its pick gate
    picked: the idea card (named by the decision, or titled as its choice), else the choice
    itself (a pick of a whole list); with no pick, its only idea, else its publish record or
    metadata.md. A review or approve gate's artifact never names a run. A run that ranked ideas
    and picked none is not listed. Each line says the title, whether it was published (with its
    outcome) or is done or in the making, and its gist: the subject's text fields (a study, a
    fact, a hook) and the start of its body. Read from the files alone, so it holds when the
    graph is behind."""
    everything = library(world)
    by_path = {a.path: a for a in everything}  # a fork's pick names its parent's card
    by_run: dict[str, list[ArtifactFile]] = {}
    for a in everything:
        by_run.setdefault(a.run_id, []).append(a)
    lines: list[str] = []
    for run_id, found in by_run.items():
        publishes = [a for a in found if a.name == "publish.md"]
        if not publishes and run_id not in runs:
            continue
        # a connector with no title of its own records "Posted to <connector>"
        titled = [a for a in publishes if not a.title.startswith("Posted to ")]
        publish = (titled or publishes or [None])[0]
        ideas = [a for a in found if a.type == "idea"]
        pick = next(
            (a for a in found if a.type == "decision" and a.fields.get("kind") == "pick"), None
        )
        subject: ArtifactFile | None = None
        if pick is not None:
            card = by_path.get(str(pick.fields.get("artifact")))
            choice = _bare(str(pick.fields.get("choice") or ""))
            subject = card if card is not None and card.type == "idea" else None
            subject = subject or next(
                (a for a in ideas if _bare(a.title).lower() == choice.lower()), None
            )
            if subject is None and choice:
                subject = ArtifactFile(pick.name, pick.path, "pick", "", choice, {}, "", run_id)
        elif len(ideas) == 1:
            subject = ideas[0]
        if publish is not None:
            metadata = next((a for a in found if a.name == "metadata.md"), None)
            subject = subject or (titled[0] if titled else metadata or publish)
        if subject is None:
            continue
        if publish is not None:
            state = f"published {publish.created[:10]}".strip()
            metric = str(publish.fields.get("primary_metric") or "")
            value = (publish.fields.get("outcomes") or {}).get(metric)
            if isinstance(value, int | float):
                state += f", {value:,} {metric}"
            state += "".join(f", {r}" for r in retention(publish.fields.get("outcomes")))
            if publish is not subject and publish.title.lower() != _bare(subject.title).lower():
                state += f", posted as {publish.title}"
        else:
            state = "made" if runs.get(run_id) == "done" else "in the making"
        text = [
            f"{k}: {v}"
            for k, v in subject.fields.items()
            if k not in _NOT_GIST and isinstance(v, str)
        ]
        paragraphs = [p for p in re.split(r"\n\s*\n", subject.body) if p.strip()]
        text += [" ".join(p.split()) for p in paragraphs if not p.lstrip().startswith("#")][:1]
        gist = "; ".join(text)
        gist = gist if len(gist) <= GIST else gist[: GIST - 3] + "..."
        lines.append(f"- {_bare(subject.title)} ({state})" + (f": {gist}" if gist else ""))
        if len(lines) == MADE_MAX:
            break
    return lines
