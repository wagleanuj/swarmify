"""Shared kernel. Bounded contexts may import this and nothing else of each other."""

from pathlib import Path

from swarmify_kernel.events import Event
from swarmify_kernel.ids import new_id

MIGRATIONS = Path(__file__).parent / "migrations"

__all__ = ["MIGRATIONS", "Event", "new_id"]
