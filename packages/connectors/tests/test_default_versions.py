"""A default package whose files change must bump its version: the box installs a default
package only when its version is new (Connectors.install_defaults), so a change shipped under an
old version never reaches it. `connectors/versions.lock.json` records each package's version and
a hash of its shipped files (not its tests or docs). After bumping, rewrite it with
`uv run python packages/connectors/tests/test_default_versions.py`."""

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
LOCK = REPO / "connectors" / "versions.lock.json"


def _shipped(name: str) -> list[str]:
    files = subprocess.run(
        ["git", "ls-files", f"connectors/{name}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(
        f
        for f in files
        if not f.endswith(".md")
        and not Path(f).name.startswith("test_")
        and Path(f).name != "conftest.py"
    )


def current() -> dict[str, dict[str, str]]:
    """Each default package's version and the hash of its shipped files."""
    out = {}
    for manifest in sorted((REPO / "connectors").glob("*/swarmify-connector.toml")):
        name = manifest.parent.name
        digest = hashlib.sha256()
        for f in _shipped(name):
            if (REPO / f).is_file():
                digest.update(f.encode() + b"\0" + (REPO / f).read_bytes())
        version = tomllib.loads(manifest.read_text())["version"]
        out[name] = {"version": version, "files": digest.hexdigest()}
    return out


def test_a_default_package_whose_files_changed_bumped_its_version() -> None:
    try:
        now = current()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    locked = json.loads(LOCK.read_text())
    unbumped = [
        n
        for n, v in now.items()
        if n in locked and v["files"] != locked[n]["files"] and v["version"] == locked[n]["version"]
    ]
    assert not unbumped, f"bump the version of {', '.join(unbumped)} in swarmify-connector.toml"
    assert now == locked, f"rewrite {LOCK.name}: run this file with python (see its docstring)"


if __name__ == "__main__":
    LOCK.write_text(json.dumps(current(), indent=2) + "\n")
