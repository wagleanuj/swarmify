"""Worlds context: a World is one long-lived memory plus the connectors and cast allowed in it.

Each world owns a folder under `<data_dir>/worlds/<id>` (files are memory, ADR 0001), with an
empty `assets/` for the operator's reusable files, and a row plus an event stream in SQLite
(state). Deleting a world removes both. Its settings (monthly budget, run cap, retention, agent
count, models, skin) and the edits to its Cast live on the row and in `cast_edits`; every change
is an event on the world's stream. Sprite packs, the art skins draw the cast with, are
installed once per install (`packs`).
"""

import re
import shutil
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmify_kernel import Event, new_id
from swarmify_worlds import cast
from swarmify_worlds.cast import JUDGE, MAX_AGENTS, MAX_PROMPT, PLANNER, worker
from swarmify_worlds.model import (
    AGENT_COUNT,
    DEFAULT_PACK,
    MODEL_ROLES,
    RETENTION_DAYS,
    RUN_CAP,
    VIEWS,
    CastEdit,
    CastMember,
    Record,
    Skin,
    View,
    World,
)
from swarmify_worlds.packs import BUNDLED, PACK_ID, Casting, Character, SpritePack, SpritePacks
from swarmify_worlds.ports import Storage
from swarmify_worlds.sqlite import MIGRATIONS, SETTINGS, SqliteStorage

__all__ = [
    "AGENT_COUNT",
    "BUNDLED",
    "DEFAULT_PACK",
    "JUDGE",
    "MAX_AGENTS",
    "MAX_PROMPT",
    "MAX_RETENTION_DAYS",
    "MAX_RUN_CAP",
    "MIGRATIONS",
    "MODEL_ROLES",
    "PLANNER",
    "RETENTION_DAYS",
    "RUN_CAP",
    "VIEWS",
    "View",
    "SETTINGS",
    "CastEdit",
    "Casting",
    "Character",
    "CastMember",
    "Record",
    "Skin",
    "SpritePack",
    "SpritePacks",
    "SqliteStorage",
    "Storage",
    "World",
    "Worlds",
    "worker",
]

MAX_RETENTION_DAYS = 3650
MODEL_ID = re.compile(r"[\w.:\-\[\]@/]{1,100}")
"""A model id: one word of up to 100 characters."""
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
        elif key == "skin":
            check_skin(value)
        elif key in SETTINGS[4:]:
            if value is not None and not (isinstance(value, str) and MODEL_ID.fullmatch(value)):
                raise ValueError(f"a model is a model id such as claude-opus-5-5, got {value!r}")
        else:
            raise ValueError(f"not a setting: {key}")


LOOK_LIMITS = {"character": 64, "name": 24, "colour": 7, "catchphrase": 80}
"""The most characters each field of an agent's look in a skin may have."""
COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


def check_skin(skin: object) -> None:
    """Raise ValueError for a skin with unknown keys, a bad view or pack id, or an agent's look
    with unknown keys, empty or over-long text or a colour that is not #rrggbb."""
    if not (isinstance(skin, dict) and set(skin) <= {"view", "pack", "cast"}):
        raise ValueError("a skin has only a view, a pack and a cast")
    if skin.get("view", "plain") not in VIEWS:
        raise ValueError(f"a view is {' or '.join(VIEWS)}, got {skin['view']!r}")
    if not (isinstance(pack := skin.get("pack", DEFAULT_PACK), str) and PACK_ID.fullmatch(pack)):
        raise ValueError(f"a pack is a sprite pack id, got {pack!r}")
    looks = skin.get("cast", {})
    if not isinstance(looks, dict):
        raise ValueError("a skin's cast is keyed by agent name")
    for agent, look in looks.items():
        if not (isinstance(look, dict) and set(look) <= set(LOOK_LIMITS)):
            raise ValueError(f"{agent}'s look has only {', '.join(LOOK_LIMITS)}")
        for field, text in look.items():
            if not (isinstance(text, str) and 0 < len(text) <= LOOK_LIMITS[field]):
                raise ValueError(f"{agent}'s {field} is 1 to {LOOK_LIMITS[field]} characters")
        if "colour" in look and not COLOUR.fullmatch(look["colour"]):
            raise ValueError(f"{agent}'s colour is #rrggbb, got {look['colour']!r}")


class Worlds:
    def __init__(
        self,
        storage: Storage,
        data_dir: Path,
        on_create: Callable[[str], None] = lambda _world_id: None,
    ) -> None:
        """`on_create` hears each new world's id once it exists, as the default packages it is
        given (ADR 0023)."""
        self.storage = storage
        self.data_dir = data_dir
        self.on_create = on_create

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
        (self.folder(world.id) / "assets").mkdir(parents=True)  # the operator's files, empty
        self.on_create(world.id)
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
        `agent_count`, `model`, the per-role `<role>_model` and `skin`, replaced whole). Raises
        ValueError for a value out of range; None for an unknown world. Shrinking the cast drops
        the looks of the agents it leaves out of the skin."""
        check_settings(changes)
        if {"skin", "agent_count"} & set(changes) and (world := self.get(world_id)) is not None:
            agents = dict(cast.names(changes.get("agent_count", world.agent_count)))
            if "skin" in changes:
                if strangers := sorted(set(changes["skin"].get("cast", {})) - set(agents)):
                    raise ValueError(f"not in the cast: {', '.join(strangers)}")
            elif set(world.skin.cast) - set(agents):
                looks = {
                    agent: {k: v for k, v in asdict(look).items() if v is not None}
                    for agent, look in world.skin.cast.items()
                    if agent in agents
                }
                skin = {"view": world.skin.view, "pack": world.skin.pack, "cast": looks}
                changes = {**changes, "skin": skin}
        event = Event.new("world.updated", world_id=world_id, payload={"id": world_id, **changes})
        return self.storage.update_settings(world_id, changes, event)

    def cast(self, world_id: str, agent_count: int | None = None) -> list[CastMember] | None:
        """The world's cast at its current versions, each with its record. `agent_count` (at
        most MAX_AGENTS) replaces the world's for one run's stage. None for an unknown world."""
        world = self.get(world_id)
        if world is None:
            return None
        records = cast.records(self.events_after(world_id))
        count = min(agent_count or world.agent_count, MAX_AGENTS)
        return cast.members(count, self.storage.cast_edits(world_id), records)

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

    def events_after(self, world_id: str | None, after_seq: int = 0) -> list[Event]:
        return self.storage.events_after(world_id, after_seq)

    def last_seq(self) -> int:
        return self.storage.last_seq()
