"""HTTP face of the Worlds context: CRUD plus a per-world SSE event stream (ADR 0002)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints

from swarmify_worlds import World, Worlds

POLL_SECONDS = 0.2
KEEPALIVE_SECONDS = 15


class NewWorld(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


def router(worlds: Worlds, on_delete: Callable[[str], None]) -> APIRouter:
    """`on_delete` lets the composition root clear other contexts' rows for a deleted world."""
    api = APIRouter(prefix="/api/worlds", tags=["worlds"])

    @api.post("", status_code=201)
    def create_world(body: NewWorld) -> World:
        return worlds.create(body.name, body.purpose)

    @api.get("")
    def list_worlds() -> list[World]:
        return worlds.list_all()

    @api.get("/{world_id}")
    def get_world(world_id: str) -> World:
        world = worlds.get(world_id)
        if world is None:
            raise HTTPException(404, "world not found")
        return world

    @api.delete("/{world_id}", status_code=204)
    def delete_world(world_id: str) -> Response:
        if not worlds.delete(world_id):
            raise HTTPException(404, "world not found")
        on_delete(world_id)
        return Response(status_code=204)

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
