"""The Build Session Capability's files (ADR 0018): what a package's Manifest `[build_session]`
supplies, read from its folder, and the build folder the core seeds for the session.

`system` and `brief` are files of the package; the brief and each of its `notes` (a file per
input file name, added when the stage has an input of that name) are `string.Template` text, with
`$input` (the stage's first input), `$folder` (the build folder) and each of the stage's params,
and in a note `$file` and `$name`, the input it is for. `arguments` are filled the same way.
"""

import shutil
from pathlib import Path
from string import Template
from typing import Any

BASE = "base"
"""The param naming one of the package's `templates` to seed from instead of its starter."""


def fill(text: str, values: dict[str, Any]) -> str:
    """`text` with each `$name` it uses of `values`; others stay as written."""
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})


def task(
    manifest: dict[str, Any], package: Path, values: dict[str, Any], inputs: list[Path]
) -> str:
    """The package's brief for this stage, then the note of each input it has one for."""
    session = manifest["build_session"]
    notes = session.get("notes", {})
    parts = [fill((package / session["brief"]).read_text(), values)]
    for path in inputs:
        if path.name in notes:
            note = (package / notes[path.name]).read_text()
            parts.append(fill(note, {**values, "file": path, "name": path.name}))
    return "\n".join(p.strip() for p in parts)


def template(base: str, data_dir: Path, templates: str) -> Path:
    """A build's `base`: a template by bare name under `<data dir>/<templates>/`, never a path,
    since whatever it names is copied where the agent can read it. Raises ValueError."""
    root = data_dir / templates
    if Path(base).name != base or base in (".", ".."):
        raise ValueError(f"base must be a template name under {root}, not a path")
    if not (root / base).is_dir():
        raise ValueError(f"the template {base} does not exist in {root}")
    return root / base


def seed(folder: Path, source: Path) -> None:
    """A fresh build folder: a copy of `source` without its node_modules or git history."""
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(source, folder, ignore=shutil.ignore_patterns("node_modules", ".git"))


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
