"""HTTP face of the Worlds context: CRUD plus a per-world SSE event stream (ADR 0002)."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints

from swarmify_worlds import World, Worlds

POLL_SECONDS = 0.2
KEEPALIVE_SECONDS = 15


class NewWorld(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class WorldOut(BaseModel):
    id: str
    name: str
    purpose: str
    created_at: str

    @staticmethod
    def of(world: World) -> "WorldOut":
        return WorldOut(**asdict(world))


def router(worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api/worlds", tags=["worlds"])

    @api.post("", status_code=201)
    def create_world(body: NewWorld) -> WorldOut:
        return WorldOut.of(worlds.create(body.name, body.purpose))

    @api.get("")
    def list_worlds() -> list[WorldOut]:
        return [WorldOut.of(w) for w in worlds.list_all()]

    @api.get("/{world_id}")
    def get_world(world_id: str) -> WorldOut:
        world = worlds.get(world_id)
        if world is None:
            raise HTTPException(404, "world not found")
        return WorldOut.of(world)

    @api.delete("/{world_id}", status_code=204)
    def delete_world(world_id: str) -> Response:
        if not worlds.delete(world_id):
            raise HTTPException(404, "world not found")
        return Response(status_code=204)

    @api.get("/{world_id}/events", response_class=StreamingResponse)
    async def world_events(world_id: str, request: Request) -> StreamingResponse:
        """Server-sent events for one world. `Last-Event-ID` resumes after a reconnect.

        The stream is not tied to the row, so a `world.deleted` event still reaches subscribers.
        """
        after = int(request.headers.get("last-event-id", 0))
        return StreamingResponse(
            _stream(worlds, world_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


async def _stream(worlds: Worlds, world_id: str, after: int) -> AsyncIterator[str]:
    # ponytail: polls SQLite every 200 ms; swap for an in-process notify when latency matters.
    idle = 0.0
    while True:
        events = worlds.events_after(world_id, after)
        for event in events:
            after = event.seq or after
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(asdict(event))}\n\n"
        idle = 0.0 if events else idle + POLL_SECONDS
        if idle >= KEEPALIVE_SECONDS:
            idle = 0.0
            yield ": keepalive\n\n"
        await asyncio.sleep(POLL_SECONDS)
