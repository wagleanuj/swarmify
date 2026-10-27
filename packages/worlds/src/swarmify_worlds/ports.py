"""Storage port of the Worlds context (ADR 0012). SQLite is the adapter; Postgres is the seam."""

from typing import Any, Protocol

from swarmify_kernel import Event
from swarmify_worlds.model import CastEdit, World


class Storage(Protocol):
    def add_world(self, world: World, event: Event) -> Event:
        """Insert the world and its `world.created` event in one transaction."""
        ...

    def list_worlds(self) -> list[World]: ...

    def get_world(self, world_id: str) -> World | None: ...

    def delete_world(self, world_id: str, event: Event) -> bool:
        """Delete the row and record `event` in one transaction. False if it did not exist."""
        ...

    def update_settings(self, world_id: str, changes: dict[str, Any], event: Event) -> World | None:
        """Write the given setting columns and record `event` in one transaction. None if the
        world does not exist."""
        ...

    def cast_edits(self, world_id: str, name: str | None = None) -> list[CastEdit]:
        """Every stored edit of the world's cast (or of one agent), oldest first."""
        ...

    def add_cast_edit(self, world_id: str, edit: CastEdit, event: Event) -> CastEdit:
        """Append the edit and record `event` in one transaction."""
        ...

    def events_after(self, world_id: str, after_seq: int) -> list[Event]: ...
