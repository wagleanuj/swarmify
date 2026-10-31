"""Sprite packs: the art a skin draws a world's cast with, installed once, picked per world.

A pack is a folder named by its id holding a `pack.toml` manifest and one subfolder per
character of transparent PNG frames. Packs come from roots searched in order, a later root
winning a shared id: the bundled pack shipped here (Kenney Toon Characters, CC0) and the
install's `<data_dir>/sprite-packs`, where the operator copies their own. A broken pack is still
listed, with what is wrong, so it can be fixed instead of drawing blank characters.
"""

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

BUNDLED = Path(__file__).parent / "sprite_packs"
MANIFEST = "pack.toml"
PACK_ID = re.compile(r"[\w-]{1,64}")


@dataclass(frozen=True)
class Character:
    name: str
    poses: dict[str, list[str]]
    """Pose name to its frame files in the character's folder, in order. Idle is required; a
    skin draws any other pose the character lacks with idle."""


@dataclass(frozen=True)
class Casting:
    """A pack's default look for an agent, keyed by agent name or role."""

    character: str | None = None
    name: str | None = None
    colour: str | None = None
    catchphrase: str | None = None


@dataclass(frozen=True)
class SpritePack:
    id: str
    name: str
    licence: str = ""
    author: str | None = None
    frame: list[int] | None = None
    """Width and height of every frame, feet at bottom centre."""
    characters: dict[str, Character] = field(default_factory=dict)
    casting: dict[str, Casting] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    """What is wrong with the pack: an unreadable manifest, a missing frame or idle pose."""


class SpritePacks:
    def __init__(self, *roots: Path) -> None:
        self.roots = roots

    def _folders(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for root in self.roots:
            if root.is_dir():
                for folder in sorted(root.iterdir()):
                    if folder.is_dir() and PACK_ID.fullmatch(folder.name):
                        found[folder.name] = folder
        return found

    def list_all(self) -> list[SpritePack]:
        return [load(pack_id, folder) for pack_id, folder in self._folders().items()]

    def file(self, pack_id: str, path: str) -> Path | None:
        """One file of a pack, or None when there is none or the path leaves the pack's
        folder (`..`, an absolute path or a symlink out)."""
        folder = self._folders().get(pack_id)
        if folder is None:
            return None
        root = folder.resolve()
        target = (root / path).resolve()
        return target if target.is_relative_to(root) and target.is_file() else None


def load(pack_id: str, folder: Path) -> SpritePack:
    """Read a pack's manifest and check its frames are there."""
    try:
        manifest = tomllib.loads((folder / MANIFEST).read_text())
        pack = _parse(pack_id, manifest)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return SpritePack(pack_id, pack_id, problems=[f"unreadable manifest: {exc}"])
    problems = list(pack.problems)
    root = folder.resolve()
    for cid, character in pack.characters.items():
        if not character.poses.get("idle"):
            problems.append(f"{cid} has no idle pose")
        for pose, frames in character.poses.items():
            for frame in frames:
                path = (root / cid / frame).resolve()
                if not (path.is_relative_to(root) and path.is_file()):
                    problems.append(f"{cid} {pose}: missing frame {frame}")
    for agent, casting in pack.casting.items():
        if casting.character is not None and casting.character not in pack.characters:
            problems.append(f"casting for {agent}: no character {casting.character}")
    return replace(pack, problems=problems)


def _parse(pack_id: str, data: dict[str, Any]) -> SpritePack:
    problems = []
    if data.get("id", pack_id) != pack_id:
        problems.append(f"the manifest says id {data['id']!r}, its folder is {pack_id!r}")
    characters = {
        cid: Character(
            _text(c.get("name", cid), "a character name"),
            {
                pose: [_text(f, "a frame file") for f in _list(fs)]
                for pose, fs in _table(c.get("poses", {})).items()
            },
        )
        for cid, c in ((k, _table(v)) for k, v in _table(data.get("characters", {})).items())
    }
    keys = {"character", "name", "colour", "catchphrase"}
    casting = {}
    for agent, entry in _table(data.get("casting", {})).items():
        entry = _table(entry)
        if unknown := set(entry) - keys:
            raise ValueError(f"casting for {agent} has unknown keys {sorted(unknown)}")
        casting[agent] = Casting(**{k: _text(v, k) for k, v in entry.items()})
    frame = data.get("frame")
    if frame is not None and not (
        isinstance(frame, list)
        and len(frame) == 2
        and all(isinstance(n, int) and n > 0 for n in frame)
    ):
        raise ValueError("frame is [width, height] in pixels")
    author = data.get("author")
    return SpritePack(
        pack_id,
        _text(data.get("name", pack_id), "name"),
        _text(data.get("licence", ""), "licence"),
        None if author is None else _text(author, "author"),
        frame,
        characters,
        casting,
        problems,
    )


def _table(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a table, got {value!r}")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"a pose is a list of frame files, got {value!r}")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} is text, got {value!r}")
    return value
