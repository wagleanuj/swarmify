"""Storage port of the Notifications context (ADR 0012). SQLite is the adapter."""

from typing import Protocol


class Storage(Protocol):
    def get_webhook(self, world_id: str) -> str | None: ...

    def set_webhook(self, world_id: str, url: str | None) -> None:
        """None removes the world's Channel."""
        ...
