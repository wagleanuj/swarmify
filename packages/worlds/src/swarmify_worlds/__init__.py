"""Worlds context: a World is one long-lived memory plus the connectors and cast allowed in it.

Each world owns a folder under `<data_dir>/worlds/<id>` (files are memory, ADR 0001) and a row
plus an event stream in SQLite (state). Deleting a world removes both. Its settings (monthly
budget, run cap, retention, agent count) and the edits to its Cast live on the row and in
`cast_edits`; every change is an event on the world's stream.
"""

import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmify_kernel import Event, new_id
from swarmify_worlds import cast
from swarmify_worlds.cast import JUDGE, MAX_AGENTS, MAX_PROMPT, PLANNER, worker
from swarmify_worlds.model import (
    AGENT_COUNT,
    RETENTION_DAYS,
    RUN_CAP,
    CastEdit,
    CastMember,
    Record,
    World,
)
from swarmify_worlds.ports import Storage
from swarmify_worlds.sqlite import MIGRATIONS, SETTINGS, SqliteStorage

__all__ = [
    "AGENT_COUNT",
    "JUDGE",
    "MAX_AGENTS",
    "MAX_PROMPT",
    "MAX_RETENTION_DAYS",
    "MAX_RUN_CAP",
    "MIGRATIONS",
    "PLANNER",
    "RETENTION_DAYS",
    "RUN_CAP",
    "SETTINGS",
    "CastEdit",
    "CastMember",
    "Record",
    "SqliteStorage",
    "Storage",
    "World",
    "Worlds",
    "worker",
]

MAX_RETENTION_DAYS = 3650
MAX_RUN_CAP = 8


def _now() -> str:
    return datetime.now(UTC).isoformat()


def check_settings(changes: dict[str, Any]) -> None:
    """Raise ValueError for a setting outside its range."""
    for key, value in changes.items():
        if key == "monthly_budget_usd":
            if value is not None and not (isinstance(value, int | float) and value > 0):
                raise ValueError("a monthly budget is a positive amount, or empty for none")
        elif key == "run_cap":
            if not (isinstance(value, int) and 1 <= value <= MAX_RUN_CAP):
                raise ValueError(f"the run cap is 1 to {MAX_RUN_CAP}, got {value}")
        elif key == "retention_days":
            if not (isinstance(value, int) and 0 <= value <= MAX_RETENTION_DAYS):
                raise ValueError(f"retention is 0 to {MAX_RETENTION_DAYS} days, got {value}")
        elif key == "agent_count":
            if not (isinstance(value, int) and 1 <= value <= MAX_AGENTS):
                raise ValueError(f"the agent count is 1 to {MAX_AGENTS}, got {value}")
        else:
            raise ValueError(f"not a setting: {key}")


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
        world = World(new_id("world"), name, purpose, _now())
        event = Event.new(
            "world.created",
            world_id=world.id,
            payload={"id": world.id, "name": name, "purpose": purpose},
        )
        self.storage.add_world(world, event)
        self.folder(world.id).mkdir(parents=True)
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

    def configure(self, world_id: str, changes: dict[str, Any]) -> World | None:
        """Change the given settings (`monthly_budget_usd`, `run_cap`, `retention_days`,
        `agent_count`). Raises ValueError for a value out of range; None for an unknown world."""
        check_settings(changes)
        event = Event.new("world.updated", world_id=world_id, payload={"id": world_id, **changes})
        return self.storage.update_settings(world_id, changes, event)

    def cast(self, world_id: str) -> list[CastMember] | None:
        """The world's cast at its current versions, each with its record. None for an unknown
        world."""
        world = self.get(world_id)
        if world is None:
            return None
        records = cast.records(self.events_after(world_id))
        return cast.members(world.agent_count, self.storage.cast_edits(world_id), records)

    def cast_versions(self, world_id: str, name: str) -> list[CastEdit] | None:
        """Every version of one agent, oldest first, version 1 being the default. None when
        the world or the agent does not exist."""
        world = self.get(world_id)
        if world is None or name not in dict(cast.names(world.agent_count)):
            return None
        default = CastEdit(name, 1, "", None, world.created_at)
        return [default, *self.storage.cast_edits(world_id, name)]

    def edit_cast(
        self, world_id: str, name: str, prompt: str, tools: list[str] | None
    ) -> CastMember | None:
        """Append a version of the agent's prompt and tools. Raises ValueError when nothing
        changed or the input is out of range; None for an unknown world or agent."""
        versions = self.cast_versions(world_id, name)
        if versions is None:
            return None
        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT:
            raise ValueError(f"a prompt is at most {MAX_PROMPT} characters")
        if tools is not None:
            tools = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
        last = versions[-1]
        if (prompt, tools) == (last.prompt, last.tools):
            raise ValueError("nothing changed")
        edit = CastEdit(name, last.number + 1, prompt, tools, _now())
        world = self.get(world_id)
        assert world is not None
        (member,) = [m for m in cast.members(world.agent_count, [edit]) if m.name == name]
        event = Event.new("cast.updated", world_id=world_id, payload=asdict(member))
        self.storage.add_cast_edit(world_id, edit, event)
        return member

    def events_after(self, world_id: str, after_seq: int = 0) -> list[Event]:
        return self.storage.events_after(world_id, after_seq)
