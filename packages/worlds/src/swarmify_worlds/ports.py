"""Storage port of the Worlds context (ADR 0012). SQLite is the adapter; Postgres is the seam."""

from typing import Protocol

from swarmify_kernel import Event
from swarmify_worlds.model import World


class Storage(Protocol):
    def add_world(self, world: World, event: Event) -> Event:
        """Insert the world and its `world.created` event in one transaction."""
        ...

    def list_worlds(self) -> list[World]: ...

    def get_world(self, world_id: str) -> World | None: ...

    def delete_world(self, world_id: str, event: Event) -> bool:
        """Delete the row and record `event` in one transaction. False if it did not exist."""
        ...

    def events_after(self, world_id: str, after_seq: int) -> list[Event]: ...
