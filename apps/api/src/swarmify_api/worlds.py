"""HTTP face of the Worlds context: CRUD, settings, the Cast and a per-world SSE event stream
(ADR 0002)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from swarmify_worlds import (
    DEFAULT_PACK,
    MAX_AGENTS,
    MAX_PROMPT,
    MAX_RETENTION_DAYS,
    MAX_RUN_CAP,
    CastEdit,
    CastMember,
    SpritePack,
    SpritePacks,
    View,
    World,
    Worlds,
)

POLL_SECONDS = 0.2
KEEPALIVE_SECONDS = 15


class NewWorld(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class LookSetting(BaseModel):
    """An agent's look in the skin only: code and logs keep the plain agent name (ADR 0004)."""

    model_config = ConfigDict(extra="forbid")

    character: str | None = None
    """A character id in the world's pack."""
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)] | None
    ) = None
    colour: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    catchphrase: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None
    ) = None
    """Said when a run ships."""


class SkinSetting(BaseModel):
    """How the web app draws the world; unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    view: View = "plain"
    pack: str = DEFAULT_PACK
    """The installed sprite pack the world draws its cast with (`GET /api/sprite-packs`)."""
    cast: dict[str, LookSetting] = {}
    """Each agent's look by agent name, over the pack's casting; an agent left out wears the
    pack's. A character the pack lacks is kept and reported by the skin."""


class WorldSettings(BaseModel):
    """The settings to change; a field left out keeps its value."""

    monthly_budget_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    """The world's spend ceiling per calendar month; null removes it."""
    run_cap: int | None = Field(default=None, ge=1, le=MAX_RUN_CAP)
    """How many of the world's runs execute at once."""
    retention_days: int | None = Field(default=None, ge=0, le=MAX_RETENTION_DAYS)
    """Days run binaries (videos, images) are kept; their markdown stays forever. 0 keeps all."""
    agent_count: int | None = Field(default=None, ge=1, le=MAX_AGENTS)
    """How many workers the cast has; a swarm stage starts up to that many."""
    model: str | None = None
    """The model the world's sessions run on; null for the global default (`GET /api/models`)."""
    planner_model: str | None = None
    worker_model: str | None = None
    judge_model: str | None = None
    retro_model: str | None = None
    """A role's model, over the world's; null for the world's. A swarm stage's `model` wins
    over both."""
    skin: SkinSetting | None = None
    """How the web app draws the world, replaced whole."""


class CastChange(BaseModel):
    """A new version of an agent's prompt and tools. `confirm` must be true: the change applies
    to every stage that starts from now on, in every run of this world."""

    prompt: str = Field(default="", max_length=MAX_PROMPT)
    tools: list[str] | None = None
    """Stage tool names the agent may use; null allows whatever the stage allows."""
    confirm: bool = False


def router(
    worlds: Worlds,
    delete: Callable[[str], bool],
    on_update: Callable[[World], None] = lambda _world: None,
) -> APIRouter:
    """`delete` is the composition root's world delete, which clears every context's rows;
    `on_update` tells it a world's settings changed (the run cap reaches the Runner)."""
    api = APIRouter(prefix="/api/worlds", tags=["worlds"])

    def world_or_404(world_id: str) -> World:
        world = worlds.get(world_id)
        if world is None:
            raise HTTPException(404, "world not found")
        return world

    @api.post("", status_code=201)
    def create_world(body: NewWorld) -> World:
        return worlds.create(body.name, body.purpose)

    @api.get("")
    def list_worlds() -> list[World]:
        return worlds.list_all()

    @api.get("/{world_id}")
    def get_world(world_id: str) -> World:
        return world_or_404(world_id)

    @api.patch("/{world_id}")
    def update_settings(world_id: str, body: WorldSettings) -> World:
        """Change the settings given; `monthly_budget_usd: null` removes the ceiling."""
        changes = {k: getattr(body, k) for k in body.model_fields_set}
        if body.skin is not None:
            changes["skin"] = body.skin.model_dump(exclude_none=True)
        try:
            world = worlds.configure(world_id, changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if world is None:
            raise HTTPException(404, "world not found")
        on_update(world)
        return world

    @api.delete("/{world_id}", status_code=204)
    def delete_world(world_id: str) -> Response:
        if not delete(world_id):
            raise HTTPException(404, "world not found")
        return Response(status_code=204)

    @api.get("/{world_id}/cast")
    def get_cast(world_id: str) -> list[CastMember]:
        """The world's agents at their current versions, each with its record: tasks done,
        first-try acceptance, send-backs and estimated cost, folded from the world's events."""
        cast = worlds.cast(world_id)
        if cast is None:
            raise HTTPException(404, "world not found")
        return cast

    @api.put("/{world_id}/cast/{name}", responses={404: {}, 422: {}})
    def edit_cast(world_id: str, name: str, body: CastChange) -> CastMember:
        """Append a version of the agent's prompt and tools. Refused (422) without `confirm`,
        when nothing changed, or when the input is out of range."""
        if not body.confirm:
            raise HTTPException(
                422, "confirm the change: it applies to every stage that starts from now on"
            )
        try:
            member = worlds.edit_cast(world_id, name, body.prompt, body.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if member is None:
            raise HTTPException(404, "no such agent in this world")
        return member

    @api.get("/{world_id}/cast/{name}/versions")
    def cast_versions(world_id: str, name: str) -> list[CastEdit]:
        """Every version of the agent's prompt and tools, oldest first; version 1 is the
        default."""
        versions = worlds.cast_versions(world_id, name)
        if versions is None:
            raise HTTPException(404, "no such agent in this world")
        return versions

    @api.get("/{world_id}/events", response_class=StreamingResponse)
    async def world_events(
        world_id: str, last_event_id: Annotated[int | None, Header()] = None, since: Since = None
    ) -> StreamingResponse:
        """Server-sent events for one world. `Last-Event-ID` resumes after a reconnect;
        without it `?since=now` starts at the current head instead of replaying history.

        A world that never existed is a 404. A deleted one still streams, so `world.deleted`
        reaches its subscribers.
        """
        if worlds.get(world_id) is None and not worlds.events_after(world_id, 0):
            raise HTTPException(404, "world not found")
        return StreamingResponse(
            _stream(worlds, world_id, _start(worlds, last_event_id, since)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


def events_router(worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["worlds"])

    @api.get("/events", response_class=StreamingResponse)
    async def all_events(
        last_event_id: Annotated[int | None, Header()] = None, since: Since = None
    ) -> StreamingResponse:
        """Server-sent events for every world, for views that span worlds such as the gates
        inbox. One stream instead of one per world: browsers cap HTTP/1.1 connections per host
        at six, and a stream per world held every slot once there were six worlds."""
        return StreamingResponse(
            _stream(worlds, None, _start(worlds, last_event_id, since)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


def packs_router(packs: SpritePacks) -> APIRouter:
    api = APIRouter(prefix="/api/sprite-packs", tags=["worlds"])

    @api.get("")
    def list_packs() -> list[SpritePack]:
        """The installed sprite packs, the bundled one first, each with its manifest and what is
        wrong with it (an unreadable manifest, a missing frame or idle pose)."""
        return packs.list_all()

    @api.get("/{pack_id}/{path:path}", response_class=FileResponse, responses={404: {}})
    def pack_file(pack_id: str, path: str) -> FileResponse:
        """One file of a pack, such as a frame `<character>/<file>`; never outside its folder."""
        file = packs.file(pack_id, path)
        if file is None:
            raise HTTPException(404, "no such file in this pack")
        return FileResponse(file)

    return api


# EventSource cannot set headers on its first connect, so live-only views ask with `?since=now`.
Since = Literal["now"] | None


def _start(worlds: Worlds, last_event_id: int | None, since: Since) -> int:
    """The seq a new stream starts after. A reconnect's Last-Event-ID always wins, even 0, so a
    view opened with `?since=now` still resumes where it left off rather than skipping a gap."""
    if last_event_id is not None:
        return last_event_id
    return worlds.last_seq() if since == "now" else 0


async def _stream(worlds: Worlds, world_id: str | None, after: int) -> AsyncIterator[str]:
    # ponytail: each open stream polls SQLite every 200 ms with a synchronous query on the event
    # loop. Fine for one operator; swap for an in-process notify when streams or latency grow.
    # An id-only frame sets the browser's lastEventId without firing an event, so a stream that
    # drops before its first event still reconnects with Last-Event-ID instead of a fresh head.
    yield f"id: {after}\n\n"
    idle = 0.0
    while True:
        events = worlds.events_after(world_id, after)
        for event in events:
            after = event.seq  # type: ignore[assignment]  # rows read back always carry seq
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(asdict(event))}\n\n"
        idle = 0.0 if events else idle + POLL_SECONDS
        if idle >= KEEPALIVE_SECONDS:
            idle = 0.0
            yield ": keepalive\n\n"
        await asyncio.sleep(POLL_SECONDS)
