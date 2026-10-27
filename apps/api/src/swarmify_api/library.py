"""HTTP face of the Library (Memory context): every artifact of a world across its runs, each
with the run and stage that made it so the page can filter by type, workflow, stage, run and
date; and Pins, the flag that makes an artifact required reading for later swarm stages."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from swarmify_memory import ArtifactFile, Memory
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


def router(memory: Memory, worlds: Worlds, list_runs: Callable[[str], list[Run]]) -> APIRouter:
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
