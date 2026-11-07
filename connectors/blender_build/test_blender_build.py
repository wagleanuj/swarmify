"""The blender_build package: what its Manifest gives the core's Build Session (ADR 0018), the
Blender brief, the voice-first note, the system prompt and the starter. The session itself, the
render after it through blender_render, its stills and the revisions are tested through the API
(apps/api/tests/test_build_http.py). With a real `blender` and `ffmpeg` on PATH the starter
renders for real through blender_render."""

import importlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import build_session, read_manifest

HERE = Path(__file__).parent
MANIFEST = read_manifest(HERE)
STARTER = HERE / MANIFEST["build_session"]["starter"]


def _task(*inputs: Path) -> str:
    values = {"quality": "draft", "input": inputs[0], "folder": HERE / "build"}
    return build_session.task(MANIFEST, HERE, values, list(inputs))


def test_it_requests_a_build_session_and_renders_through_blender_render() -> None:
    session = MANIFEST["build_session"]
    assert MANIFEST["capabilities"] == ["build_session", "templates"] and "command" not in MANIFEST
    assert MANIFEST["enable_by_default"] is False
    assert (session["connector"], session["tool"], session["output"]) == (
        "blender_render",
        "blender_render",
        "video.mp4",
    )
    assert session["tools"] == ["stills"]
    assert session["arguments"] == {"project": "$folder", "quality": "$quality"}
    assert "network" not in MANIFEST.get("permissions", {})  # the session reaches no network
    render = read_manifest(HERE.parent / "blender_render")
    assert MANIFEST["owned_folders"] == render["owned_folders"]  # one folder of Templates for both
    assert {k: MANIFEST["templates"][k] for k in render["templates"]} == render["templates"]
    assert MANIFEST["templates"]["guide"] == "TEMPLATE.md"
    params = MANIFEST["params"]["properties"]
    assert set(params) == {"base", "brief", "model", "max_turns", "minutes", "quality", "tools"}
    assert params["quality"]["default"] == render["params"]["properties"]["quality"]["default"]


def test_the_brief_names_the_script_the_folder_and_the_quality(tmp_path: Path) -> None:
    script = tmp_path / "script.md"
    brief = _task(script)
    assert f"turns the script at {script}" in brief and str(HERE / "build") in brief
    assert "quality `draft`" in brief and "render.json" in brief


def test_speech_timings_name_the_narration_by_absolute_path(tmp_path: Path) -> None:
    script, timings = tmp_path / "script.md", tmp_path / "speech" / "timings.json"
    plain = _task(script, tmp_path / "notes.md")
    assert "voice first" not in plain
    voiced = _task(script, timings)
    assert f"{timings} is the narration" in voiced
    assert f"{timings.parent}/narration/" in voiced and "do not copy them" in voiced
    assert "cut_in" in voiced and "joined_next" in voiced and "BLENDER_OUTPUT" in voiced


def test_the_builder_checks_every_shot_with_stills_and_never_renders() -> None:
    system = (HERE / MANIFEST["build_session"]["system"]).read_text()
    for must in (
        "Blender is not in your Bash",
        "mcp__blender_render__stills",
        "first, key and last frames",
        "read the contact sheet",
        "Keep it current",
        "Never run Blender, the full render or the finish step yourself",
        "BLENDER_SHOT_FRAMES",
        "frame_NNNN.png",
        "guide",
    ):
        assert must in system, must


def test_the_starter_is_a_one_shot_project_with_no_finish() -> None:
    spec = json.loads((STARTER / "render.json").read_text())
    assert [s["id"] for s in spec["shots"]] == ["s01"] and "finish" not in spec
    assert set(spec["quality"]) == {"draft", "final"}
    script = (STARTER / spec["shots"][0]["script"]).read_text()
    for env in ("BLENDER_SHOT_OUT", "BLENDER_SHOT_FRAMES", "BLENDER_SHOT_QUALITY"):
        assert env in script, env
    compile(script, "s01.py", "exec")


@pytest.mark.skipif(
    not (shutil.which("blender") and shutil.which("ffmpeg")),
    reason="a real render needs blender and ffmpeg on PATH",
)
def test_the_starter_renders_as_is_with_real_blender(tmp_path: Path) -> None:
    """The starter, copied as a build is, renders through blender_render's own job into a video
    of its 60 frames."""
    sys.path.insert(0, str(HERE.parent / "blender_render"))
    br: Any = importlib.import_module("blender_render")

    project = tmp_path / "stage" / "build"
    shutil.copytree(STARTER, project)
    output = tmp_path / "stage" / "artifacts" / "video.mp4"
    job = Path(
        br.blender_render(idempotency_key="k", output_path=str(output), project=str(project))[
            "job_id"
        ]
    )
    deadline = time.monotonic() + 300
    while br.blender_render_status(str(job))["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.2)
    state = json.loads(job.read_text())
    assert state["status"] == "done", br._tail(Path(state["log"]))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=nb_read_frames,width,height", "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == "1080,1920,60"
