"""The Build Session Capability's files (ADR 0018): what a package's Manifest `[build_session]`
supplies, read from its folder, and the build folder the core seeds for the session.

`system` and `brief` are files of the package; the brief and each of its `notes` (a file per
input file name, added when the stage has an input of that name) are `string.Template` text, with
`$input` (the stage's first input), `$folder` (the build folder) and each of the stage's params,
and in a note `$file` and `$name`, the input it is for. `arguments` are filled the same way.
A stage's `base` names a Template of the world (ADR 0028) to seed from instead of the starter.
"""

import hashlib
import os
import shutil
from pathlib import Path
from string import Template
from typing import Any

BASE = "base"
"""The param naming one of the world's Templates of the package to seed from, not its starter."""
RECORD = ".template.json"
"""A Template's own record, in its folder: where it came from and when it was added."""


def fill(text: str, values: dict[str, Any]) -> str:
    """`text` with each `$name` it uses of `values`; others stay as written."""
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})


def task(
    manifest: dict[str, Any],
    package: Path,
    values: dict[str, Any],
    inputs: list[Path],
    template: str = "",
) -> str:
    """The package's brief for this stage, then the note of each input it has one for, then,
    when the build starts from the Template `template` and it has the package's guide file, a
    line telling the builder to read that first."""
    session = manifest["build_session"]
    notes = session.get("notes", {})
    parts = [fill((package / session["brief"]).read_text(), values)]
    for path in inputs:
        if path.name in notes:
            note = (package / notes[path.name]).read_text()
            parts.append(fill(note, {**values, "file": path, "name": path.name}))
    guide = manifest.get("templates", {}).get("guide")
    if template and guide and (Path(values["folder"]) / guide).is_file():
        parts.append(
            f"Read {Path(values['folder']) / guide} first: the guide of the Template {template}"
            " this build starts from."
        )
    return "\n".join(p.strip() for p in parts)


def template(manifest: dict[str, Any], world: Path, base: str) -> Path:
    """A build's `base`: a Template by bare name in the world folder's Templates folder of the
    package, never a path, since whatever it names is copied where the agent can read it.
    Raises ValueError."""
    if Path(base).name != base or base.startswith("."):
        raise ValueError(f"base must be a Template's name, not a path: {base}")
    found = world / "assets" / manifest["templates"]["folder"] / base
    if found.is_symlink() or not found.is_dir():
        label = manifest["label"]
        raise ValueError(f"the Template {base} is not one of this world's {label} Templates")
    return found


def fingerprint(folder: Path) -> str:
    """A Template's content hash: its files' paths and bytes, without node_modules or its record,
    so the same project has the same fingerprint wherever it is and whatever was installed."""
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != "node_modules")
        for name in sorted(files):
            path = Path(root) / name
            rel = path.relative_to(folder).as_posix()
            if rel == RECORD or path.is_symlink():
                continue
            data = path.read_bytes()
            digest.update(f"{rel}\0{len(data)}\0".encode())
            digest.update(data)
    return digest.hexdigest()


def seed(folder: Path, source: Path) -> None:
    """A fresh build folder: a copy of `source` without its node_modules, git history, record
    as a Template or symlinks (a Template is world data, so a link in it is not followed out of
    it into the builder's writable folder)."""
    skip = shutil.ignore_patterns("node_modules", ".git", RECORD)

    def ignore(root: str, names: list[str]) -> set[str]:
        return set(skip(root, names)) | {n for n in names if (Path(root) / n).is_symlink()}

    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(source, folder, ignore=ignore)


def keep(folder: Path, version: Path, output: Path | None) -> None:
    """A revision of the last build: that build is kept as `version` and the file it made, if
    any, beside it as `version` plus the file's suffix (once, so a retried revision starts from
    the same point), then `folder` starts over as a copy of `version` to edit."""
    if not version.exists():
        folder.rename(version)
        if output is not None and output.is_file():
            shutil.copy2(output, version.with_name(version.name + output.suffix))
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(version, folder, ignore=shutil.ignore_patterns("node_modules"))
