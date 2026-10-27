"""HTTP face of the Worlds context: CRUD, settings, the Cast and a per-world SSE event stream
(ADR 0002)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from swarmify_worlds import (
    MAX_AGENTS,
    MAX_PROMPT,
    MAX_RETENTION_DAYS,
    MAX_RUN_CAP,
    CastEdit,
    CastMember,
    World,
    Worlds,
)

POLL_SECONDS = 0.2
KEEPALIVE_SECONDS = 15


class NewWorld(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


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


class CastChange(BaseModel):
    """A new version of an agent's prompt and tools. `confirm` must be true: the change applies
    to every stage that starts from now on, in every run of this world."""

    prompt: str = Field(default="", max_length=MAX_PROMPT)
    tools: list[str] | None = None
    """Stage tool names the agent may use; null allows whatever the stage allows."""
    confirm: bool = False


def router(
    worlds: Worlds,
    on_delete: Callable[[str], None],
    on_update: Callable[[World], None] = lambda _world: None,
) -> APIRouter:
    """`on_delete` lets the composition root clear other contexts' rows for a deleted world;
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
        if not worlds.delete(world_id):
            raise HTTPException(404, "world not found")
        on_delete(world_id)
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
        world_id: str, last_event_id: Annotated[int, Header()] = 0
    ) -> StreamingResponse:
        """Server-sent events for one world. `Last-Event-ID` resumes after a reconnect.

        A world that never existed is a 404. A deleted one still streams, so `world.deleted`
        reaches its subscribers.
        """
        if worlds.get(world_id) is None and not worlds.events_after(world_id, 0):
            raise HTTPException(404, "world not found")
        return StreamingResponse(
            _stream(worlds, world_id, last_event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


async def _stream(worlds: Worlds, world_id: str, after: int) -> AsyncIterator[str]:
    # ponytail: each open stream polls SQLite every 200 ms with a synchronous query on the event
    # loop. Fine for one operator; swap for an in-process notify when streams or latency grow.
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
