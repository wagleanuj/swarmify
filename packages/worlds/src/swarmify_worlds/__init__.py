"""Worlds context: a World is one long-lived memory plus the connectors and cast allowed in it.

Each world owns a folder under `<data_dir>/worlds/<id>` (files are memory, ADR 0001) and a row
plus an event stream in SQLite (state). Deleting a world removes both.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from swarmify_kernel import Event, new_id
from swarmify_worlds.model import World
from swarmify_worlds.ports import Storage
from swarmify_worlds.sqlite import MIGRATIONS, SqliteStorage

__all__ = ["MIGRATIONS", "SqliteStorage", "Storage", "World", "Worlds"]


class Worlds:
    def __init__(self, storage: Storage, data_dir: Path) -> None:
        self.storage = storage
        self.data_dir = data_dir

    def folder(self, world_id: str) -> Path:
        return self.data_dir / "worlds" / world_id

    def create(self, name: str, purpose: str = "") -> World:
        name, purpose = name.strip(), purpose.strip()
        if not name:
            raise ValueError("a world needs a name")
        world = World(new_id("world"), name, purpose, datetime.now(UTC).isoformat())
        self.folder(world.id).mkdir(parents=True)
        event = Event.new(
            "world.created",
            world_id=world.id,
            payload={"id": world.id, "name": name, "purpose": purpose},
        )
        self.storage.add_world(world, event)
        return world

    def list_all(self) -> list[World]:
        return self.storage.list_worlds()

    def get(self, world_id: str) -> World | None:
        return self.storage.get_world(world_id)

    def delete(self, world_id: str) -> bool:
        event = Event.new("world.deleted", world_id=world_id, payload={"id": world_id})
        if not self.storage.delete_world(world_id, event):
            return False
        shutil.rmtree(self.folder(world_id), ignore_errors=True)
        return True

    def events_after(self, world_id: str, after_seq: int = 0) -> list[Event]:
        return self.storage.events_after(world_id, after_seq)
