"""The hyperframes_build package: what its Manifest gives the core's Build Session (ADR 0018), the
HyperFrames brief, the system prompt and the offline starter. The session itself, the render after
it through hyperframes_render and the workflow check are tested through the API
(apps/api/tests/test_build_http.py)."""

import json
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from swarmify_connectors import build_session, read_manifest

HERE = Path(__file__).parent
MANIFEST = read_manifest(HERE)
STARTER = HERE / MANIFEST["build_session"]["starter"]
VOICE_RULES = ["data-media-start", "data-automation", "white-space: pre", "node voice.mjs"]


def _task(*inputs: Path) -> str:
    values = {"composition": "index.html", "input": inputs[0], "folder": HERE / "build"}
    return build_session.task(MANIFEST, HERE, values, list(inputs))


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
    brief = _task(script)
    assert f"composition `index.html` turns the script at {script}" in brief
    assert "window.__hyperframes.getVariables()" in brief


def test_speech_timings_make_the_brief_voice_first(tmp_path: Path) -> None:
    script, timings = tmp_path / "script.md", tmp_path / "timings.json"
    plain = _task(script, tmp_path / "notes.md")
    assert not any(rule in plain for rule in VOICE_RULES)
    voiced = _task(script, timings)
    assert all(rule in voiced for rule in VOICE_RULES)
    assert (
        f"{timings} is the narration" in voiced and "Copy timings.json into the project" in voiced
    )
    assert "```js\n" in voiced  # a sample composition to adapt


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


@pytest.mark.skipif(not shutil.which("npm"), reason="no npm here")
def test_the_voice_first_sample_writes_a_composition_the_linter_accepts(tmp_path: Path) -> None:
    """The note's sample, run in the starter on a timings.json in the speech connector's shape,
    writes an index.html whose length is the voice timeline's, with each line a trimmed clip of
    its take, and HyperFrames' own linter accepts it."""
    folder = tmp_path / "build"
    shutil.copytree(STARTER, folder, ignore=shutil.ignore_patterns("node_modules"))
    voiced = _task(tmp_path / "script.md", tmp_path / "timings.json")
    sample = re.search(r"```js\n(.*?)```", voiced, re.S)
    assert sample is not None
    (folder / "voice.mjs").write_text(sample[1])
    (folder / "narration").mkdir()
    with wave.open(str(folder / "narration/hook.wav"), "wb") as take:
        take.setparams((1, 2, 24000, 0, "NONE", "not compressed"))  # 3 s of silence
        take.writeframes(b"\0\0" * 24000 * 3)
    (folder / "bed.mp3").write_bytes(b"")  # a bed from the world's assets
    word = {"timestampMs": None, "confidence": None}
    line = {"directions": [], "unrecognized": [], "joined_next": False}
    lines = [
        {
            **line,
            "text": "Shh! The <owl> sleeps.",
            "pause_after": 0.3,
            "cut_in": 0.0,
            "cut_out": 1.6,
            "words": [
                {**word, "text": t, "startMs": s, "endMs": s + 300}
                for t, s in (("Shh!", 50), (" The", 500), (" <owl>", 800), (" sleeps.", 1100))
            ],
        },
        {
            **line,
            "text": "Good night",
            "pause_after": 0.5,
            "cut_in": 1.6,
            "cut_out": 2.8,
            "words": [
                {**word, "text": t, "startMs": s, "endMs": s + 400}
                for t, s in (("Good", 1700), (" night", 2100))
            ],
        },
    ]
    take = {"scene": "hook", "file": "narration/hook.wav", "duration": 3.0, "lines": lines}
    (folder / "timings.json").write_text(json.dumps({"takes": [take]}))
    env = {**os.environ, "npm_config_cache": str(tmp_path / "npm-cache")}
    install = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    for argv in (install, ["node", "voice.mjs"], ["npm", "run", "lint"]):
        done = subprocess.run(argv, cwd=folder, capture_output=True, text=True, env=env)
        assert done.returncode == 0, done.stdout + done.stderr
    assert "0 errors" in done.stdout, done.stdout
    page = (folder / "index.html").read_text()
    # 0.4 s lead, the two clips and their pauses, 0.6 s tail
    assert 'data-composition-id="main" data-start="0" data-duration="4.6"' in page
    assert re.findall(
        r'<audio id="line-\d" src="narration/hook.wav" data-start="([\d.]+)" '
        r'data-duration="([\d.]+)" data-media-start="([\d.]+)"',
        page,
    ) == [
        ("0.4", "1.6", "0"),
        ("2.3", "1.2", "1.6"),
    ]
    assert 'src="bed.mp3"' in page and "&lt;owl&gt;" in page
