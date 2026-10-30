"""What the `remotion_build` stage keeps of Remotion in the core until it becomes a package
(issue 217): the starter project and the template rules its build folder is seeded by. The render
itself is the remotion_render package's (`connectors/remotion_render`, issue 216).
"""

import shutil
from pathlib import Path

STARTER = Path(__file__).with_name("remotion_starter")
"""The minimal Remotion project a build starts from when it names no template."""


def template(base: str, data_dir: Path) -> Path:
    """A build's `base`: a template by bare name under `<data dir>/remotion/`, never a path,
    since whatever it names is copied where the agent can read it. Raises ValueError."""
    if Path(base).name != base or base in (".", ".."):
        raise ValueError(f"base must be a template name under {data_dir / 'remotion'}, not a path")
    folder = data_dir / "remotion" / base
    if not folder.is_dir():
        raise ValueError(f"the Remotion template {base} does not exist in {folder.parent}")
    return folder


def seed(folder: Path, base: str | None, data_dir: Path) -> None:
    """A fresh build folder: a copy of the template `base` (see `template`), or of the starter,
    without its node_modules or git history."""
    source = template(base, data_dir) if base else STARTER
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(source, folder, ignore=shutil.ignore_patterns("node_modules", ".git"))


def keep(folder: Path, version: Path, video: Path) -> None:
    """A revision of the last build: that project is kept as `version` and the video it
    rendered beside it as `version` plus the video's suffix (once, so a retried revision starts
    from the same point), then `folder` starts over as a copy of `version` to edit."""
    if not version.exists():
        folder.rename(version)
        if video.is_file():
            shutil.copy2(video, version.with_name(version.name + video.suffix))
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(version, folder, ignore=shutil.ignore_patterns("node_modules"))
