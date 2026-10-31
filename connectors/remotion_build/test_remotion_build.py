"""The remotion_build package: what its Manifest gives the core's Build Session (ADR 0018), the
Remotion brief, the voice-first note, the 3D guidance and the starter. The session itself, the
render after it and the revisions are tested through the API (apps/api/tests/test_build_http.py)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmify_connectors import build_session, read_manifest

HERE = Path(__file__).parent
MANIFEST = read_manifest(HERE)
VOICE_RULES = ["createTikTokStyleCaptions", "white-space: pre", "trimBefore", "durationInFrames"]


def _task(*inputs: Path) -> str:
    values = {"composition": "Main", "input": inputs[0], "folder": HERE / "build"}
    return build_session.task(MANIFEST, HERE, values, list(inputs))


def test_it_requests_a_build_session_and_renders_through_remotion_render() -> None:
    session = MANIFEST["build_session"]
    assert MANIFEST["capabilities"] == ["build_session"] and "command" not in MANIFEST
    assert (session["connector"], session["tool"], session["output"]) == (
        "remotion_render",
        "remotion_render",
        "video.mp4",
    )
    assert session["arguments"] == {"project": "$folder", "composition": "$composition"}
    assert MANIFEST["permissions"]["network"] == [
        "registry.npmjs.org",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    ]


def test_the_brief_names_the_script_and_the_composition(tmp_path: Path) -> None:
    script = tmp_path / "script.md"
    brief = _task(script)
    assert f"composition `Main` turns the script at {script}" in brief
    assert "getInputProps()" in brief and "public/" in brief


def test_speech_timings_make_the_brief_voice_first(tmp_path: Path) -> None:
    script, timings = tmp_path / "script.md", tmp_path / "timings.json"
    plain = _task(script, tmp_path / "notes.md")
    assert not any(rule in plain for rule in VOICE_RULES)
    voiced = _task(script, timings)
    assert all(rule in voiced for rule in VOICE_RULES)
    assert f"{timings} is the narration" in voiced and "Copy timings.json into public/" in voiced
    assert "```tsx\n" in voiced  # the spike's composition, as a sample


def test_the_builder_knows_3d_is_available_and_how_to_keep_it_deterministic() -> None:
    """A 3D scene renders locally and in Docker (spike 2026-10-30), if every frame is a function
    of the frame number and every asset is in the project."""
    system = (HERE / MANIFEST["build_session"]["system"]).read_text()
    for must in (
        "@remotion/three",
        "<ThreeCanvas>",
        "useCurrentFrame()",
        "never React Three",
        "world's assets",
    ):
        assert must in system, must


@pytest.mark.skipif(not shutil.which("npm"), reason="no npm here")
def test_the_starter_builds_with_the_voice_first_example(tmp_path: Path) -> None:
    """The starter installs from its lockfile and type-checks with the note's sample as its Main,
    reading a timings.json in the speech connector's shape. The starter ships @remotion/captions."""
    folder = tmp_path / "build"
    shutil.copytree(HERE / "starter", folder, ignore=shutil.ignore_patterns("node_modules"))
    assert "@remotion/captions" in json.loads((folder / "package.json").read_text())["dependencies"]
    note = (HERE / MANIFEST["build_session"]["notes"]["timings.json"]).read_text()
    sample = re.search(r"```tsx\n(.*?)```", note, re.S)
    assert sample is not None
    (folder / "src/Main.tsx").write_text(sample[1])
    word = {"startMs": 0, "endMs": 400, "timestampMs": None, "confidence": None}
    line = {"pause_after": 0.3, "cut_in": 0.0, "cut_out": 0.5, "joined_next": False}
    take = {"scene": "hook", "file": "narration/hook.wav", "duration": 0.6}
    take["lines"] = [{**line, "text": "Shh!", "words": [{**word, "text": "Shh!"}]}]
    (folder / "public").mkdir()
    (folder / "public/timings.json").write_text(json.dumps({"takes": [take]}))
    for argv in (["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], ["npx", "tsc"]):
        done = subprocess.run(argv, cwd=folder, capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
