"""Skill files: the shared folder `<data>/skills/<name>/SKILL.md` every world reads, and each
world's own `worlds/<id>/skills/<name>/SKILL.md`, which wins on conflict.

Agents load only the world folder, as a local plugin (ADR 0016), so `sync` links every shared
skill the world has not overridden into the world's skills folder. A link is how a shared skill
is told apart from a world's own; writing a world version replaces the link with a real folder.
"""

import shutil
from pathlib import Path

from swarmify_learning.model import Skill, Source
from swarmify_learning.retro import frontmatter

SKILL_FILE = "SKILL.md"


class SkillFiles:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.shared = data_dir / "skills"

    def world_dir(self, world_id: str) -> Path:
        return self.data_dir / "worlds" / world_id / "skills"

    def shared_names(self) -> list[str]:
        return sorted(p.parent.name for p in self.shared.glob(f"*/{SKILL_FILE}") if p.is_file())

    def sync(self, world_id: str) -> None:
        """Link shared skills the world lacks; drop links whose shared skill is gone."""
        # ponytail: symlinks, so both folders must share a filesystem (they do: one data dir).
        # Copy instead if the data dir ever spans volumes.
        folder = self.world_dir(world_id)
        if not folder.parent.is_dir():
            return
        folder.mkdir(exist_ok=True)
        for link in folder.iterdir():
            if link.is_symlink() and not (link / SKILL_FILE).is_file():
                link.unlink()
        for name in self.shared_names():
            target = folder / name
            if not target.exists() and not target.is_symlink():
                target.symlink_to(self.shared / name, target_is_directory=True)

    def read(self, world_id: str, name: str) -> str | None:
        path = self.world_dir(world_id) / name / SKILL_FILE
        return path.read_text() if path.is_file() else None

    def write(self, world_id: str, name: str, content: str) -> Path:
        """Write the world's own SKILL.md, replacing a link to a shared skill."""
        folder = self.world_dir(world_id) / name
        if folder.is_symlink():
            folder.unlink()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / SKILL_FILE
        path.write_text(content if content.endswith("\n") else content + "\n")
        return path

    def remove(self, world_id: str, name: str) -> None:
        """Remove the world's own skill; a shared one of that name applies again on sync."""
        folder = self.world_dir(world_id) / name
        if folder.is_symlink():
            folder.unlink()
        elif folder.is_dir():
            shutil.rmtree(folder)

    def list(self, world_id: str) -> list[Skill]:
        """Every skill the world's agents load, by name: its own, and the shared ones linked in."""
        self.sync(world_id)
        out = []
        for path in sorted(self.world_dir(world_id).glob(f"*/{SKILL_FILE}")):
            if not path.is_file():
                continue
            source: Source = "shared" if path.parent.is_symlink() else "world"
            content = path.read_text()
            out.append(
                Skill(
                    path.parent.name,
                    frontmatter(content).get("description", ""),
                    source,
                    str(path.relative_to(self.data_dir)),
                    content,
                )
            )
        return out
