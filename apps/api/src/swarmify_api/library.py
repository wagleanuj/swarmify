"""HTTP face of the Library (Memory context): every artifact of a world across its runs, each
with the run and stage that made it so the page can filter by type, workflow, stage, run and
date; Pins, the flag that makes an artifact required reading for later swarm stages; and the
world's Assets, the operator's reusable files (music beds, sound effects, logos) that agents read
and never write, with the Owned Folders of the packages it enables (ADR 0025): folders of its
assets a package gives meaning, as a folder of voices, that the Assets page shows as groups.
An Owned Folder a package's `[templates]` names holds its Templates (ADR 0028), one subfolder
each."""

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from swarmify_connectors import Call, Connector, build_session
from swarmify_memory import ASSETS, ArtifactFile, Memory
from swarmify_runs import Run
from swarmify_workflows import ConnectorStage, Workflows
from swarmify_worlds import Worlds

_DECISION_FOLDER = re.compile(r"^(\d{2})-")
_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")


def template_cap() -> int:
    """The most bytes a Template upload may unpack to: `SWARMIFY_TEMPLATE_MAX_BYTES`, else 1 GB."""
    return int(os.environ.get("SWARMIFY_TEMPLATE_MAX_BYTES") or 10**9)


@dataclass(frozen=True)
class LibraryItem:
    """An artifact as the Library lists it: the file plus where it came from."""

    path: str
    name: str
    type: str
    title: str
    author: str
    created: str
    pinned: bool
    body: str
    fields: dict[str, Any]
    run_id: str
    run_name: str
    workflow_id: str
    workflow_version: int | None
    stage_index: int | None
    stage_name: str


@dataclass(frozen=True)
class Asset:
    path: str
    """Relative to the world folder, as `/files/` serves it."""
    name: str
    bytes: int


@dataclass(frozen=True)
class OwnedFolder:
    """A folder of the world's assets a package it enables owns (Manifest `[[owned_folders]]`).
    Swarmify lists, plays, uploads, renames and deletes its files; the package gives them
    meaning."""

    path: str
    """Relative to the world folder, ending in `/`: its files are the assets under it."""
    folder: str
    """The plain name to upload into it with, as `voices`."""
    label: str
    about: str
    accept: list[str]
    """Media types an upload must be, as `audio/wav` or `image/*`; empty takes any file."""
    confirm: str
    """What the operator affirms before an upload, as the right to use a voice; "" for none."""
    package: str
    """The label of the package that owns it."""
    templates: bool
    """Whether it holds a package's Templates, one subfolder each, rather than loose files."""
    require: list[str]
    """The paths each of its Templates must hold, as its package's `[templates]` declares."""


@dataclass(frozen=True)
class Template:
    """A Template of a world: a subfolder of a Templates folder, with its record when it has one."""

    name: str
    folder: str
    """The Owned Folder's `folder` it is in."""
    path: str
    """Relative to the world folder, ending in `/`."""
    bytes: int
    origin: str
    """`upload`, `run` (saved from a run's stage), or `folder` when placed there by hand."""
    run_id: str
    stage: str
    added: str
    """ISO time it was added; for one placed by hand, its folder's modified time."""
    fingerprint: str
    """A hash of its files' paths and bytes, `node_modules` aside, from when it was added; ""
    for one placed by hand."""


def owned_folders(connectors: list[Connector]) -> list[OwnedFolder]:
    """The Owned Folders the Manifests of these connectors declare, the first package to claim
    a folder keeping it."""
    found: dict[str, OwnedFolder] = {}
    held: dict[str, list[str]] = {}
    for c in connectors:
        if declared := c.manifest.get("templates"):
            held.setdefault(declared["folder"], declared.get("require", []))
    for c in connectors:
        for f in c.manifest.get("owned_folders", []):
            found.setdefault(f["folder"], OwnedFolder(
                f"{ASSETS}/{f['folder']}/", f["folder"], f["label"], f.get("about", ""),
                f.get("accept", []), f.get("confirm", ""), c.manifest["label"],
                f["folder"] in held, held.get(f["folder"], []),
            ))  # fmt: skip
    return list(found.values())


def templates(world: Path, folders: list[OwnedFolder]) -> list[Template]:
    """The Templates in the world folder's Templates folders, by folder then name."""
    found = []
    for f in (f for f in folders if f.templates):
        for d in sorted((world / f.path).glob("*/")):
            if d.name.startswith(".") or d.is_symlink():
                continue
            files = [p for p in d.rglob("*") if p.is_file() and not p.is_symlink()]
            record = d / build_session.RECORD
            try:
                meta = json.loads(record.read_text()) if record.is_file() else {}
            except ValueError:  # a record mangled by hand: the Template is still there
                meta = {}
            found.append(Template(
                d.name, f.folder, f"{f.path}{d.name}/",
                sum(p.stat().st_size for p in files if p != record),
                meta.get("origin", "folder"), meta.get("run_id", ""), meta.get("stage", ""),
                meta.get("added") or datetime.fromtimestamp(d.stat().st_mtime, UTC).isoformat(),
                meta.get("fingerprint", ""),
            ))  # fmt: skip
    return found


def fingerprint(folder: Path) -> str:
    """A hash of the Template's files, paths and bytes, without `node_modules` or its record, so
    the same project added twice has the same one."""
    digest = hashlib.sha256()
    files = {p.relative_to(folder).as_posix(): p for p in folder.rglob("*") if p.is_file()}
    for rel, p in sorted(files.items()):
        if rel == build_session.RECORD or "node_modules" in rel.split("/") or p.is_symlink():
            continue
        with p.open("rb") as f:
            digest.update(f"{rel}\0".encode() + hashlib.file_digest(f, "sha256").digest())
    return digest.hexdigest()


def _gb(n: int) -> str:
    return f"{n / 1e9:g} GB" if n >= 10**9 else f"{n / 1e6:g} MB"


def unpack(upload: Path, into: Path, cap: int) -> Path:
    """Extract the zip `upload` into the new folder `into`, every entry checked first, and return
    the Template's root: its single top folder when it has one. Raises ValueError naming the
    problem: an entry outside the folder, a link or device file, more than `cap` bytes."""
    try:
        z = zipfile.ZipFile(upload)
    except zipfile.BadZipFile as exc:
        raise ValueError("the upload is not a zip file") from exc
    with z:
        entries = [i for i in z.infolist() if not i.filename.startswith("__MACOSX/")]
        for i in entries:
            parts = re.split(r"[/\\]", i.filename)
            if i.filename.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                raise ValueError(f"the zip holds {i.filename}, outside the Template")
            if stat.S_IFMT(i.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"the zip holds {i.filename}, a link or special file")
        if sum(i.file_size for i in entries) > cap:
            raise ValueError(f"the zip unpacks to more than the {_gb(cap)} cap")
        into.mkdir()
        try:
            for i in entries:
                z.extract(i, into)
        except (zipfile.BadZipFile, OSError) as exc:  # a size or CRC that lies, a full disk
            raise ValueError(f"the zip could not be unpacked ({exc})") from exc
    top = list(into.iterdir())
    return top[0] if len(top) == 1 and top[0].is_dir() else into


async def received(request: Request, file: Path, cap: int | None = None) -> None:
    """Stream the request body to `file`, never holding it in memory, refusing more than `cap`
    bytes."""
    size = 0
    with file.open("xb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if cap is not None and size > cap:
                raise ValueError(f"the upload is more than the {_gb(cap)} cap")
            out.write(chunk)


@dataclass(frozen=True)
class Naming:
    """A workflow whose current version names a Template, as a stage's `base` or `project`."""

    id: str
    name: str


@dataclass(frozen=True)
class TemplateDelete:
    deleted: bool
    """False until the delete is confirmed: then it only answers the workflows naming it."""
    workflows: list[Naming]


def naming(workflows: Workflows, world_id: str, calls: set[str], name: str) -> list[Naming]:
    """The world's workflows whose current version has a stage calling one of `calls` that
    names the Template `name` as its `base` (a build) or `project` (a direct render)."""
    found = []
    for w in workflows.list_all(world_id):
        version = workflows.version(w.id, w.current_version)
        stages = version.model.stages if version else []
        if any(
            isinstance(s, ConnectorStage)
            and s.calls in calls
            and name in (s.params.get("base"), s.params.get("project"))
            for s in stages
        ):
            found.append(Naming(w.id, w.name))
    return found


def zipped(folder: Path) -> Path:
    """A temporary zip of a Template's files at its root, without its record or installed
    packages, so it uploads back as the same Template."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as out:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(folder.rglob("*")):
                rel = p.relative_to(folder)
                if p.is_file() and not p.is_symlink() and "node_modules" not in rel.parts:
                    if rel != Path(build_session.RECORD):
                        z.write(p, rel.as_posix())
    return Path(out.name)


def accepted(name: str, accept: list[str]) -> bool:
    """Whether a file of this name is one of the media types `accept` lists (any when empty),
    by its extension, `audio/x-wav` counting as `audio/wav`."""
    kind = (mimetypes.guess_type(name)[0] or "").replace("/x-", "/")
    return not accept or any(
        kind == a or (a.endswith("/*") and kind.startswith(a[:-1])) for a in accept
    )


def _not_kept(folder: str, names: list[str]) -> set[str]:
    """What a saved build leaves out: installed packages, git history and links, which could
    point anywhere on the machine."""
    return {
        n for n in names if n in ("node_modules", ".git") or os.path.islink(os.path.join(folder, n))
    }


class SaveTemplateBody(BaseModel):
    name: str
    """A plain name, as an upload's."""
    replace: bool = False


class RenameBody(BaseModel):
    name: str
    """The new plain file name; the asset stays in its folder."""


class PinBody(BaseModel):
    path: str
    """The artifact's path in the world folder, as the Library lists it."""
    pinned: bool = True


def item(artifact: ArtifactFile, run: Run | None) -> LibraryItem:
    stage = None
    if run is not None:
        stage = next((s for s in run.stages if s.id == artifact.stage), None)
        if stage is None and (m := _DECISION_FOLDER.match(artifact.stage)):
            index = int(m.group(1)) - 1
            stage = run.stages[index] if 0 <= index < len(run.stages) else None
    return LibraryItem(
        artifact.path,
        artifact.name,
        artifact.type,
        artifact.title,
        artifact.author,
        artifact.created,
        artifact.pinned,
        artifact.body,
        artifact.fields,
        artifact.run_id,
        run.name if run else "",
        (run.workflow_id or "") if run else "",  # "" for an Ad Hoc Run too
        run.workflow_version if run else None,
        stage.index if stage else None,
        stage.name if stage else "",
    )


def router(
    memory: Memory,
    worlds: Worlds,
    list_runs: Callable[[str], list[Run]],
    connectors: Callable[[str], list[Connector]],
    workflows: Workflows,
    get_run: Callable[[str], Run | None],
    stage_package: Callable[[Run, str], Connector | None],
    call_for: Callable[[str], Call | None],
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["library"])

    def owned(world_id: str) -> list[OwnedFolder]:
        return owned_folders(connectors(world_id))

    def world_or_404(world_id: str) -> None:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")

    @api.get("/worlds/{world_id}/library")
    def library(world_id: str) -> list[LibraryItem]:
        """Every artifact of the world across its runs, newest first."""
        world_or_404(world_id)
        runs = {r.id: r for r in list_runs(world_id)}
        return [item(a, runs.get(a.run_id)) for a in memory.library(world_id)]

    @api.get("/worlds/{world_id}/files/{path:path}", response_class=FileResponse)
    def file(world_id: str, path: str) -> FileResponse:
        """A run file or asset as it is on disk, by its Library path: the video a gate plays,
        an image, a note a card cites, a music bed. Only files under the world's runs and
        assets are served."""
        world_or_404(world_id)
        try:
            return FileResponse(memory.file(world_id, path))
        except ValueError as exc:
            raise HTTPException(404, "file not found") from exc

    def asset(world_id: str, path: str) -> Asset:
        file = memory.world_folder(world_id) / path
        return Asset(path, file.name, file.stat().st_size)

    @api.get("/worlds/{world_id}/assets")
    def assets(world_id: str) -> list[Asset]:
        """The world's assets folder, subfolders included, by path."""
        world_or_404(world_id)
        return [asset(world_id, p) for p in memory.assets(world_id)]

    @api.get("/worlds/{world_id}/owned_folders")
    def list_owned_folders(world_id: str) -> list[OwnedFolder]:
        """The Owned Folders of the packages the world enables, each a group on the Assets
        page; their files are in the assets list under each one's `path`."""
        world_or_404(world_id)
        return owned(world_id)

    @api.get("/worlds/{world_id}/templates")
    def list_templates(world_id: str) -> list[Template]:
        """The Templates of the packages the world enables, each a subfolder of its package's
        Templates folder, those placed there by hand included."""
        world_or_404(world_id)
        return templates(memory.world_folder(world_id), owned(world_id))

    def add_template(
        world_id: str,
        f: OwnedFolder,
        name: str,
        work: Path,
        root: Path,
        replace: bool,
        record: dict[str, str],
        require: list[str],
    ) -> Template:
        """Check the Template at `root`, in the work folder `work`, for `require`, write its
        record and move it into place as `name`."""
        if missing := [r for r in require if not (root / r).exists()]:
            raise ValueError(f"the Template lacks {', '.join(missing)}, which {f.package} requires")
        files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
        (root / build_session.RECORD).write_text(json.dumps({
            **record,
            "added": datetime.now(UTC).isoformat(),
            "bytes": sum(p.stat().st_size for p in files),
            "fingerprint": fingerprint(root),
        }))  # fmt: skip
        target = work.parent / name
        if target.exists():
            if not replace:
                raise FileExistsError(name)
            target.rename(work / "replaced")  # removed with the work folder
        root.rename(target)
        world = memory.world_folder(world_id)
        return next(t for t in templates(world, [f]) if t.name == name)

    def templates_folder(world_id: str, folder: str, name: str, replace: bool) -> OwnedFolder:
        """The Templates folder `folder` of the world, once `name` is a plain name free in it
        (or `replace` is asked)."""
        f = next((f for f in owned(world_id) if f.folder == folder and f.templates), None)
        if f is None:
            raise HTTPException(422, f"{folder} is not a Templates folder of this world")
        if not _TEMPLATE_NAME.match(name):
            raise HTTPException(422, f"a Template needs a plain name like cat-shorts, got {name}")
        if (memory.world_folder(world_id) / f.path / name).exists() and not replace:
            raise taken(f, name)
        return f

    def taken(f: OwnedFolder, name: str) -> HTTPException:
        return HTTPException(409, f"{name} is already in {f.label}; replace it or pick a name")

    async def adding(
        world_id: str, f: OwnedFolder, name: str, add: Callable[[Path], Awaitable[Template]]
    ) -> Template:
        """Run `add` on a fresh work folder in the Templates folder, so a Template moves into
        place on the same disk, and answer its problems; the work folder goes either way."""
        work = memory.world_folder(world_id) / f.path / f".upload-{secrets.token_hex(8)}"
        work.mkdir(parents=True)
        try:
            return await add(work)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except FileExistsError as exc:
            raise taken(f, name) from exc
        finally:
            await run_in_threadpool(shutil.rmtree, work, True)

    @api.put(
        "/worlds/{world_id}/templates/{folder}/{name}",
        status_code=201,
        responses={404: {}, 409: {}, 422: {}},
    )
    async def upload_template(
        world_id: str, folder: str, name: str, request: Request, replace: bool = False
    ) -> Template:
        """Upload a zip as the Template `name` of the Templates folder `folder`: streamed to
        disk, its entries checked, unpacked (a single top folder unwrapped) and checked for the
        files its package requires, then moved into place. A name already there is refused
        unless `replace`."""
        world_or_404(world_id)
        f = templates_folder(world_id, folder, name, replace)

        async def add(work: Path) -> Template:
            await received(request, work / "upload.zip", template_cap())
            root = await run_in_threadpool(
                unpack, work / "upload.zip", work / "unpacked", template_cap()
            )
            record = {"origin": "upload"}
            return await run_in_threadpool(
                add_template, world_id, f, name, work, root, replace, record, f.require
            )

        return await adding(world_id, f, name, add)

    @api.post(
        "/runs/{run_id}/stages/{stage_id}/template",
        status_code=201,
        responses={404: {}, 409: {}, 422: {}},
    )
    async def save_template(run_id: str, stage_id: str, body: SaveTemplateBody) -> Template:
        """Save as Template: the build folder of a finished build stage whose package declares
        Templates, copied without its revisions, node_modules or links, the package's `strip`
        paths removed and its `require` files checked, as the Template `body.name` of the
        package's Templates folder, its origin this run and stage. A name already there is
        refused unless `body.replace`."""
        run = get_run(run_id)
        stage = next((s for s in run.stages if s.id == stage_id), None) if run else None
        if run is None or stage is None:
            raise HTTPException(404, "stage not found")
        package = stage_package(run, stage.calls or "") if stage.type == "connector" else None
        declared = package.manifest.get("templates") if package else None
        if package is None or not declared or "build_session" not in package.manifest:
            raise HTTPException(422, f"{stage.name} is not a build of a package with Templates")
        build = memory.world_folder(run.world_id) / "runs" / run.id / stage.id / "build"
        call = call_for(stage.id)
        if stage.status != "done" or call is None or call.status != "done" or not build.is_dir():
            raise HTTPException(422, f"{stage.name} has no finished build to save")
        f = templates_folder(run.world_id, declared["folder"], body.name, body.replace)

        def add(work: Path) -> Template:
            root = work / "build"
            shutil.copytree(build, root, ignore=_not_kept)
            for path in declared.get("strip", []):
                gone = root / path
                if gone.is_dir() and not gone.is_symlink():
                    shutil.rmtree(gone)
                else:
                    gone.unlink(missing_ok=True)
            record = {"origin": "run", "run_id": run.id, "stage": stage.name}
            require = declared.get("require", [])
            return add_template(
                run.world_id, f, body.name, work, root, body.replace, record, require
            )

        return await adding(run.world_id, f, body.name, lambda work: run_in_threadpool(add, work))

    def template(world_id: str, folder: str, name: str) -> Path:
        world_or_404(world_id)
        world = memory.world_folder(world_id)
        for t in templates(world, owned(world_id)):
            if (t.folder, t.name) == (folder, name):
                return world / t.path
        raise HTTPException(404, "template not found")

    @api.get(
        "/worlds/{world_id}/templates/{folder}/{name}/download",
        response_class=FileResponse,
        responses={404: {}},
    )
    def download_template(world_id: str, folder: str, name: str) -> FileResponse:
        """A Template as a zip of its files, which uploads back as the same Template."""
        out = zipped(template(world_id, folder, name))
        return FileResponse(
            out, media_type="application/zip", filename=f"{name}.zip",
            background=BackgroundTask(out.unlink),
        )  # fmt: skip

    @api.delete("/worlds/{world_id}/templates/{folder}/{name}", responses={404: {}})
    def delete_template(
        world_id: str, folder: str, name: str, confirm: bool = False
    ) -> TemplateDelete:
        """Delete a Template once `confirm`ed; until then, only answer the workflows whose
        stages name it. Past runs keep their own copies."""
        path = template(world_id, folder, name)
        calls = {
            c.name
            for c in connectors(world_id)
            if c.manifest.get("templates", {}).get("folder") == folder
        }
        named = naming(workflows, world_id, calls, name)
        if confirm:
            shutil.rmtree(path)
        return TemplateDelete(confirm, named)

    @api.put("/worlds/{world_id}/assets/{name}", status_code=201, responses={422: {}})
    async def add_asset(
        world_id: str,
        name: str,
        request: Request,
        folder: Annotated[str, Query(description="an Owned Folder's `folder`")] = "",
    ) -> Asset:
        """Upload an asset: the request body is the file, `name` a plain file name, into the
        assets folder or one of the world's Owned Folders, whose `accept` it must match. A
        name already there is replaced."""
        world_or_404(world_id)
        if folder:
            into = next((f for f in owned(world_id) if f.folder == folder), None)
            if into is None:
                raise HTTPException(422, f"{folder} is not an Owned Folder of this world")
            if not accepted(name, into.accept):
                raise HTTPException(422, f"{into.label} takes {', '.join(into.accept)} only")
        assets = memory.world_folder(world_id) / ASSETS
        assets.mkdir(parents=True, exist_ok=True)
        upload = assets / f".upload-{secrets.token_hex(8)}"
        try:
            await received(request, upload)
            path = memory.add_asset(world_id, name, upload, folder)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            upload.unlink(missing_ok=True)
        return asset(world_id, path)

    def own_asset(world_id: str, path: str) -> None:
        world_or_404(world_id)
        if path not in memory.assets(world_id):
            raise HTTPException(404, "asset not found")

    @api.delete(
        "/worlds/{world_id}/assets/{path:path}", status_code=204, responses={404: {}, 422: {}}
    )
    def remove_asset(world_id: str, path: str) -> Response:
        """Delete an asset by its path, as the list gives it."""
        own_asset(world_id, path)
        memory.remove_asset(world_id, path)
        return Response(status_code=204)

    @api.patch("/worlds/{world_id}/assets/{path:path}", responses={404: {}, 422: {}})
    def rename_asset(world_id: str, path: str, body: RenameBody) -> Asset:
        """Rename an asset in its folder. A name already there is refused, never replaced."""
        own_asset(world_id, path)
        try:
            return asset(world_id, memory.rename_asset(world_id, path, body.name))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/worlds/{world_id}/pins")
    def pin(world_id: str, body: PinBody) -> LibraryItem:
        """Pin or unpin an artifact. Pinned artifacts are injected into the planner and worker
        context of every later swarm stage of the world."""
        world_or_404(world_id)
        try:
            pinned = memory.pin(world_id, body.path, body.pinned)
        except ValueError as exc:
            raise HTTPException(404, "artifact not found") from exc
        runs = {r.id: r for r in list_runs(world_id)}
        return item(pinned, runs.get(pinned.run_id))

    return api
