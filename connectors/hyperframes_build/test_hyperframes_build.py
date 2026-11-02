"""The hyperframes_build package: what its Manifest gives the core's Build Session (ADR 0018), the
HyperFrames brief, the system prompt and the offline starter. The session itself, the render after
it through hyperframes_render and the workflow check are tested through the API
(apps/api/tests/test_build_http.py)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmify_connectors import build_session, read_manifest

HERE = Path(__file__).parent
MANIFEST = read_manifest(HERE)
STARTER = HERE / MANIFEST["build_session"]["starter"]


def test_it_requests_a_build_session_and_renders_through_hyperframes_render() -> None:
    session = MANIFEST["build_session"]
    assert MANIFEST["capabilities"] == ["build_session"] and "command" not in MANIFEST
    assert MANIFEST["enable_by_default"] is False  # Remotion stays the default engine
    assert (session["connector"], session["tool"], session["output"]) == (
        "hyperframes_render",
        "hyperframes_render",
        "video.mp4",
    )
    assert session["arguments"] == {"project": "$folder", "composition": "$composition"}
    assert session["templates"] == "hyperframes"
    assert MANIFEST["permissions"]["network"] == [
        "registry.npmjs.org",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.jsdelivr.net",
    ]
    params = MANIFEST["params"]["properties"]
    assert set(params) == {"base", "composition", "brief", "model", "max_turns", "minutes"}
    assert params["composition"]["default"] == "index.html"


def test_the_brief_names_the_script_and_the_composition(tmp_path: Path) -> None:
    script = tmp_path / "script.md"
    values = {"composition": "index.html", "input": script, "folder": tmp_path / "build"}
    brief = build_session.task(MANIFEST, HERE, values, [script])
    assert f"composition `index.html` turns the script at {script}" in brief
    assert "window.__hyperframes.getVariables()" in brief


def test_the_builder_is_told_the_render_is_offline_and_to_check_its_work() -> None:
    system = (HERE / MANIFEST["build_session"]["system"]).read_text()
    for must in (
        "data-start",
        "paused GSAP timeline",
        "getVariables()",
        "no network",
        "node_modules/gsap/dist/gsap.min.js, never a CDN",
        "hyperframes-localize-fonts",
        "npm run lint",
        "npm run check",
        "npm run snapshot",
        "never installs",
    ):
        assert must in system, must


def test_the_starter_renders_offline_and_reads_the_script_from_the_variables() -> None:
    """No CDN and no web font, so the sandboxed render needs nothing it cannot reach; the CLI
    pinned, and every HyperFrames command the builder runs with telemetry off."""
    page = (STARTER / "index.html").read_text()
    assert not re.search(r"https?://", page) and "@import" not in page
    assert '<script src="node_modules/gsap/dist/gsap.min.js">' in page
    assert "window.__hyperframes.getVariables()" in page
    package = json.loads((STARTER / "package.json").read_text())
    assert package["devDependencies"]["hyperframes"] == "0.9.0"
    assert "gsap" in package["dependencies"]
    for name in ("lint", "check", "snapshot", "fonts"):
        assert "HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1" in package["scripts"][name], name


@pytest.mark.skipif(not shutil.which("npm"), reason="no npm here")
def test_the_starter_passes_the_hyperframes_linter(tmp_path: Path) -> None:
    """The starter installs from its lockfile and HyperFrames' own linter accepts it."""
    folder = tmp_path / "build"
    shutil.copytree(STARTER, folder, ignore=shutil.ignore_patterns("node_modules"))
    env = {**os.environ, "npm_config_cache": str(tmp_path / "npm-cache")}
    install = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    for argv in (install, ["npm", "run", "lint"]):
        done = subprocess.run(argv, cwd=folder, capture_output=True, text=True, env=env)
        assert done.returncode == 0, done.stdout + done.stderr
    assert "0 errors" in done.stdout, done.stdout
