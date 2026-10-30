"""HTTP face of the Library (Memory context): every artifact of a world across its runs, each
with the run and stage that made it so the page can filter by type, workflow, stage, run and
date; Pins, the flag that makes an artifact required reading for later swarm stages; and the
world's Assets, the operator's reusable files (music beds, sound effects, logos) that agents read
and never write, with the Owned Folders of the packages it enables (ADR 0025): folders of its
assets a package gives meaning, as speech its voices, that the Assets page shows as groups."""

import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from swarmify_connectors import Connector
from swarmify_memory import ASSETS, ArtifactFile, Memory
from swarmify_runs import Run
from swarmify_worlds import Worlds

_DECISION_FOLDER = re.compile(r"^(\d{2})-")


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


def owned_folders(connectors: list[Connector]) -> list[OwnedFolder]:
    """The Owned Folders the Manifests of these connectors declare, the first package to claim
    a folder keeping it."""
    found: dict[str, OwnedFolder] = {}
    for c in connectors:
        for f in c.manifest.get("owned_folders", []):
            found.setdefault(f["folder"], OwnedFolder(
                f"{ASSETS}/{f['folder']}/", f["folder"], f["label"], f.get("about", ""),
                f.get("accept", []), f.get("confirm", ""), c.manifest["label"],
            ))  # fmt: skip
    return list(found.values())


def accepted(name: str, accept: list[str]) -> bool:
    """Whether a file of this name is one of the media types `accept` lists (any when empty),
    by its extension, `audio/x-wav` counting as `audio/wav`."""
    kind = (mimetypes.guess_type(name)[0] or "").replace("/x-", "/")
    return not accept or any(
        kind == a or (a.endswith("/*") and kind.startswith(a[:-1])) for a in accept
    )


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
    owned: Callable[[str], list[OwnedFolder]],
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["library"])

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
        # ponytail: the whole body in memory; stream it to disk if assets grow past a few
        # hundred MB.
        try:
            path = memory.add_asset(world_id, name, await request.body(), folder)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
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
