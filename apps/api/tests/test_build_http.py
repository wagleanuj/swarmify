"""A script stage then a `remotion_build` (or `hyperframes_build`, or `blender_build`) stage, end
to end through the API: real DBOS on SQLite, the real MCP transport and render wrapper, the Agent
SDK client with a scripted CLI as the builder, and a fake Remotion (or HyperFrames) CLI as the
project's own (or blender_render's fake Blender and ffmpeg on PATH),
rendering inside the OS sandbox. One real build of each, on the subscription and the real CLI,
runs when you opt in with SWARMIFY_REAL_BUILD=1."""

import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_swarm import AgentRuntime, SdkRuntime, SessionEnd, SessionHooks, SessionSpec, build
from swarmify_swarm.fake import Answer, Call, Die, Say, Script

# These tests start a default package's real `.venv` process, so its install step runs.
real_default_installs = True

HERE = Path(__file__).parent
FAKE_NPX = HERE.parents[2] / "connectors/remotion_render/fake_npx.py"
FAKE_HYPERFRAMES = HERE.parents[2] / "connectors/hyperframes_render/fake_hyperframes.py"
BLENDER_RENDER = HERE.parents[2] / "connectors/blender_render"
OFF_BY_DEFAULT = {
    "hyperframes_build": {"hyperframes_build": "1.0.1", "hyperframes_render": "1.0.1"},
    "blender_build": {"blender_build": "1.0.0", "blender_render": "1.0.4"},
}
"""Installed with Swarmify but not defaults: a world enables the pair to build with it."""
SANDBOX = shutil.which("sandbox-exec") or shutil.which("bwrap")


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Chrome Headless Shell, ffmpeg and ffprobe on PATH, as the image has the first and an
    operator running natively all three; the Remotion or HyperFrames CLI is the project's own,
    which `_installs` fakes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("chrome-headless-shell", "ffmpeg", "ffprobe"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def fake_blender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """blender_render's fake Blender, ffmpeg and ffprobe on PATH, as an operator running natively
    has the real ones."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("blender", "fake_blender.py"), ("ffmpeg", "fake_ffmpeg.py")):
        (bin_dir / name).write_text(f"#!{sys.executable}\n" + (BLENDER_RENDER / source).read_text())
        (bin_dir / name).chmod(0o755)
    os.link(bin_dir / "ffmpeg", bin_dir / "ffprobe")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("SWARMIFY_GPU_LOCK", raising=False)


@pytest.fixture
def opened() -> Iterator[list[TestClient]]:
    """Every app a test starts, shut down after it whatever happened, so DBOS stops."""
    clients: list[TestClient] = []
    yield clients
    for client in clients:
        client.__exit__(None, None, None)


def _installs(options: Any, name: str = "remotion", fake: Path = FAKE_NPX) -> None:
    """What `npm install` leaves, done by the scripted builder: the project's Remotion CLI, a
    fake one (fake_npx.py), or its HyperFrames CLI (fake_hyperframes.py)."""
    cli = Path(options.cwd) / "node_modules" / ".bin" / name
    cli.parent.mkdir(parents=True)
    cli.write_text(f"#!{sys.executable}\n" + fake.read_text())
    cli.chmod(0o755)


def _installs_hyperframes(options: Any) -> None:
    _installs(options, "hyperframes", FAKE_HYPERFRAMES)


def build_run(
    tmp_path: Path,
    runtime: AgentRuntime,
    params: str,
    opened: list[TestClient],
    inputs: str = "input: script.md",
    after: str = "",
    calls: str = "remotion_build",
    before: Callable[[Path], None] = lambda _: None,
    retro: bool = False,
    wait: bool = True,
) -> tuple[TestClient, dict]:
    """`inputs` is the build stage's input line; a notes stage writing notes.md runs before it
    when it names notes.md. `after` is YAML for stages after it; the run is returned once it
    ends or waits at a gate. `calls` is the build package; HyperFrames' are enabled first.
    `before` gets the world's folder before the run starts; `retro` ends the run with its Retro,
    whose session the script answers. Without `wait` the run is returned as it starts."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / ".env").write_text("UPLOAD_POST_API_KEY=k3y\n")
    (data / ".env").chmod(0o600)
    app = create_app(data, poll_seconds=0.05, retro=retro, runtime=runtime, extract=fake_extract)
    client = TestClient(app)
    client.__enter__()
    opened.append(client)
    w = client.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    before(data / "worlds" / w)
    server = shlex.join([sys.executable, str(HERE / "script_server.py")])
    added = client.post(
        f"/api/worlds/{w}/connectors",
        json={
            "name": "script_writer",
            "kind": "stage",
            "server": server,
            "stage_types": ["connector"],
        },
    )
    assert added.status_code == 201, added.text
    for name, version in OFF_BY_DEFAULT.get(calls, {}).items():
        enabled = client.put(f"/api/worlds/{w}/packages/{name}", json={"version": version})
        assert enabled.status_code == 200, enabled.text
    notes = "  - connector: notes\n    calls: script_writer\n    output: notes.md\n"
    yaml = f"""name: Script then build
stages:
  - connector: script
    calls: script_writer
    output: script.md
{notes if "notes.md" in inputs else ""}  - connector: video
    calls: {calls}
    {inputs}
    params: {params}
{after}"""
    workflow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
    assert workflow.status_code == 201, workflow.text
    run = client.post(f"/api/workflows/{workflow.json()['workflow_id']}/runs", json={}).json()
    return client, settled(client, run["id"]) if wait else run


def settled(client: TestClient, run_id: str, send_backs: int = 0) -> dict:
    """The run once it ended or waits at a gate, after `send_backs` send-backs."""
    deadline = time.monotonic() + float(os.environ.get("SWARMIFY_BUILD_WAIT", 60))
    while True:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["send_backs"] == send_backs and run["status"] in ("done", "failed", "waiting_gate"):
            return run
        assert time.monotonic() < deadline, run
        time.sleep(0.1)


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_the_builder_writes_the_project_and_the_render_makes_the_video(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    script = Script(
        [
            Call("Bash", {"command": "npm install"}),
            Answer(_installs),
            Call("Write", {"file_path": "src/Main.tsx", "content": "export const Main = 1"}),
            Call("Write", {"file_path": str(tmp_path / "data/.env"), "content": "X=1"}),
            Say("A title card over black."),
        ],
        cost=0.37,
    )
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{brief: Loud.}", opened
    )
    assert run["status"] == "done", run
    script_id, video_id = run["stages"][0]["id"], run["stages"][1]["id"]
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    video = folder / video_id / "artifacts/video.mp4"
    rendered = json.loads(video.read_text())
    assert rendered["composition"] == "Main" and rendered["script"].startswith("# Why the chase")
    assert rendered["scriptPath"] == str(folder / script_id / "artifacts/script.md")
    assert (folder / video_id / "artifacts/video.mp4.md").is_file()

    # the session: in the build folder, sandboxed, no keys, the hooks refusing the write out
    (options,) = script.sessions
    project = folder / video_id / "build"
    assert options.cwd == project
    assert (options.sandbox or {}).get("allowUnsandboxedCommands") is False
    assert options.env["UPLOAD_POST_API_KEY"] == "" and options.env["SWARMIFY_PASSWORD"] == ""
    assert [o.is_error for o in script.outcomes] == [False, False, True]
    assert (tmp_path / "data/.env").read_text() == "UPLOAD_POST_API_KEY=k3y\n"
    assert "Loud." in script.prompts[0]

    # the folder stays for a person to open, its node_modules gone; the render ran after
    assert (project / "src/index.ts").is_file() and not (project / "node_modules").exists()
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert call["connector"] == "remotion_render" and call["request"]["project"] == str(project)
    [artifact] = client.get(f"/api/runs/{run['id']}/stages/{video_id}/artifacts").json()
    assert (artifact["name"], artifact["type"]) == ("video.mp4", "video")
    assert run["spent_usd"] == pytest.approx(0.37)  # the builder's session, on the run budget


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_build_with_several_inputs_renders_the_first_and_names_the_rest(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    script = Script([Call("Bash", {"command": "npm install"}), Answer(_installs), Say("Done.")])
    runtime = SdkRuntime(transport=script.transport)
    client, run = build_run(tmp_path, runtime, "{}", opened, "inputs: [script.md, notes.md]")
    assert run["status"] == "done", run
    script_id, notes_id, video_id = (s["id"] for s in run["stages"])
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    rendered = json.loads((folder / video_id / "artifacts/video.mp4").read_text())
    assert rendered["scriptPath"] == str(folder / script_id / "artifacts/script.md")
    assert str(folder / notes_id / "artifacts/notes.md") in script.prompts[0]


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_send_back_revises_the_previous_project_with_the_note_kept_as_a_version(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    def writes(name: str) -> Answer:  # the scripted CLI runs no tools, so the step writes
        return Answer(lambda options: (Path(options.cwd) / "src" / name).write_text("1"))

    liked, edit = writes("Liked.tsx"), writes("Ending.tsx")
    script = Script([Answer(_installs), liked, Say("v1")], [Answer(_installs), edit, Say("v2")])
    review = "  - gate: review\n    ask: Good?\n"
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened, after=review
    )
    assert run["status"] == "waiting_gate", run
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    first = (stage / "artifacts/video.mp4").read_text()
    assert "revision" not in script.prompts[0]

    note = "Slow the ending down; keep the opening."
    sent = client.post(
        f"/api/runs/{run['id']}/decision", json={"note": note, "send_back_to": "video"}
    )
    assert sent.status_code == 200, sent.text
    run = settled(client, run["id"], send_backs=1)
    assert run["status"] == "waiting_gate", run

    # the second session edits the kept project, told it revises and what to read first
    assert [o.cwd for o in script.sessions] == [stage / "build"] * 2
    prompt = script.prompts[1]
    assert prompt.startswith("This is a revision, not a fresh start.") and note in prompt
    assert f"{note} Change what the note asks and keep the rest as it is." in prompt
    assert f"before you change anything: {stage}/build.v1, {stage}/build.v1.mp4." in prompt
    assert (stage / "build/src/Liked.tsx").is_file() and (stage / "build/src/Ending.tsx").is_file()
    # the previous version stays beside it, inside the stage folder the session may read
    assert (stage / "build.v1/src/Liked.tsx").is_file()
    assert not (stage / "build.v1/src/Ending.tsx").exists()
    assert (stage / "build.v1.mp4").read_text() == first
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert call["visit"] == 1 and call["request"]["project"] == str(stage / "build")


def test_a_failed_build_fails_the_stage_with_the_last_log_lines(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    script = Script([Call("Bash", {"command": "npx tsc --noEmit"}), Say("Gave up.")])

    class Failing(SdkRuntime):
        async def run(self, spec: Any, hooks: Any) -> Any:
            end = await super().run(spec, hooks)
            return type(end)(end.session_id, end.cost_usd, True, "tsc found 3 errors")

    client, run = build_run(tmp_path, Failing(transport=script.transport), "{}", opened)
    assert run["status"] == "failed"
    error = run["stages"][1]["error"]
    assert "builder failed 2 sessions in a row" in error and "tsc found 3 errors" in error
    assert "tool: Bash" in error
    assert client.get(f"/api/stages/{run['stages'][1]['id']}/call").status_code == 404


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_failed_render_fails_the_stage_with_the_cli_output(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    script = Script([Answer(_installs), Say("Done.")])
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{composition: Broken}", opened
    )
    assert run["status"] == "failed"
    error = run["stages"][1]["error"]
    assert "remotion render exited with 1" in error and "No composition with the ID Broken" in error


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_hyperframes_build_writes_the_project_and_hyperframes_render_makes_the_video(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    script = Script(
        [
            Call("Bash", {"command": "npm install"}),
            Answer(_installs_hyperframes),
            Say("A title card over black."),
        ]
    )
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened, calls="hyperframes_build"
    )
    assert run["status"] == "done", run
    script_id, video_id = run["stages"][0]["id"], run["stages"][1]["id"]
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    project = folder / video_id / "build"
    rendered = json.loads((folder / video_id / "artifacts/video.mp4").read_text())
    assert rendered["project"] == str(project) and rendered["flags"]["composition"] == "index.html"
    assert rendered["script"].startswith("# Why the chase")
    assert rendered["scriptPath"] == str(folder / script_id / "artifacts/script.md")
    assert rendered["env"]["HYPERFRAMES_NO_TELEMETRY"] == "1"
    assert (folder / video_id / "artifacts/video.mp4.md").is_file()

    # the session: seeded from the starter, told HyperFrames, reaching only its own hosts
    (options,) = script.sessions
    assert options.cwd == project and "HyperFrames" in str(options.system_prompt)
    hosts = ["registry.npmjs.org", "fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net"]
    assert (options.sandbox or {}).get("network", {}).get("allowedDomains") == hosts
    assert f"composition `index.html` turns the script at {folder / script_id}" in script.prompts[0]
    starter = HERE.parents[2] / "connectors/hyperframes_build/starter/index.html"
    assert (project / "index.html").read_text() == starter.read_text()
    assert not (project / "node_modules").exists()
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert call["connector"] == "hyperframes_render" and call["request"]["project"] == str(project)


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_hyperframes_build_starts_from_a_world_template_the_check_finds(
    tmp_path: Path, fake_cli: None, opened: list[TestClient]
) -> None:
    def promo(world: Path) -> None:
        folder = world / "assets/hyperframes_templates/promo"
        folder.mkdir(parents=True)
        (folder / "index.html").write_text("<!-- promo -->\n")

    script = Script([Answer(_installs_hyperframes), Say("Done.")])
    runtime = SdkRuntime(transport=script.transport)
    client, run = build_run(
        tmp_path, runtime, "{base: promo}", opened, calls="hyperframes_build", before=promo
    )
    assert run["status"] == "done", run
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    assert (folder / run["stages"][1]["id"] / "build/index.html").read_text() == "<!-- promo -->\n"

    yaml = (
        "name: Typo\nstages:\n  - connector: video\n    calls: hyperframes_build\n"
        "    input: script.md\n    params: {base: promu}\n"
    )
    checked = client.post(f"/api/worlds/{run['world_id']}/workflows/check", json={"yaml": yaml})
    missing = "the Template promu is not one of this world's HyperFrames build Templates"
    assert any(missing in v["message"] for v in checked.json()["violations"]), checked.text


@pytest.mark.skipif(not os.environ.get("SWARMIFY_REAL_BUILD"), reason="set SWARMIFY_REAL_BUILD=1")
def test_a_real_build_renders_a_tiny_composition(tmp_path: Path, opened: list[TestClient]) -> None:
    """The real model on the subscription writes a real Remotion project, and Remotion renders
    it. Several minutes: npm installs and Chrome downloads once, into the temp data directory."""
    os.environ.setdefault("SWARMIFY_BUILD_WAIT", "1800")
    brief = "A 2 second video at 15 fps, 360x640: the script's title on a plain background."
    client, run = build_run(
        tmp_path, SdkRuntime(), f"{{brief: {json.dumps(brief)}, max_turns: 40}}", opened
    )
    assert run["status"] == "done", run["stages"][1].get("error")
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    video = folder / run["stages"][1]["id"] / "artifacts/video.mp4"
    assert video.stat().st_size > 1000 and video.read_bytes()[4:8] == b"ftyp"
    print(f"\nreal build: ${run['spent_usd']:.2f}, video {video}")


@pytest.mark.skipif(not os.environ.get("SWARMIFY_REAL_BUILD"), reason="set SWARMIFY_REAL_BUILD=1")
def test_a_real_hyperframes_build_renders_offline(tmp_path: Path, opened: list[TestClient]) -> None:
    """The real model on the subscription writes a real HyperFrames project from the starter, and
    hyperframes_render's real install renders it in its sandbox with no network. Several minutes:
    npm installs, ffmpeg and Chrome download once, into the temp data directory."""
    os.environ.setdefault("SWARMIFY_BUILD_WAIT", "1800")
    brief = "A 2 second video, 360x640: the script's title on a plain background."
    params = f"{{brief: {json.dumps(brief)}, max_turns: 40}}"
    client, run = build_run(tmp_path, SdkRuntime(), params, opened, calls="hyperframes_build")
    assert run["status"] == "done", run["stages"][1].get("error")
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    video = folder / run["stages"][1]["id"] / "artifacts/video.mp4"
    assert video.stat().st_size > 1000 and video.read_bytes()[4:8] == b"ftyp"
    print(f"\nreal hyperframes build: ${run['spent_usd']:.2f}, video {video}")


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_blender_build_previews_with_stills_and_blender_render_makes_the_video(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """Issue 407: seeded from the starter, offered blender_render's stills (never its render),
    then blender_render renders the build folder at the stage's quality into video.mp4."""
    script = Script([Say("A cube turns in the dark.")])
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened, calls="blender_build"
    )
    assert run["status"] == "done", run
    script_id, video_id = run["stages"][0]["id"], run["stages"][1]["id"]
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    project = folder / video_id / "build"
    starter = BLENDER_RENDER.parent / "blender_build/starter"
    assert (project / "render.json").read_text() == (starter / "render.json").read_text()
    rendered = json.loads((folder / video_id / "artifacts/video.mp4").read_text())
    assert rendered["inputs"] == [str(folder / video_id / "shots/s01.mp4")]  # joined, no finish
    assert (folder / video_id / "artifacts/video.mp4.md").is_file()
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert call["connector"] == "blender_render"
    assert (call["request"]["project"], call["request"]["quality"]) == (str(project), "draft")
    [artifact] = client.get(f"/api/runs/{run['id']}/stages/{video_id}/artifacts").json()
    assert (artifact["name"], artifact["type"]) == ("video.mp4", "video")

    (options,) = script.sessions
    assert options.cwd == project and "stills" in str(options.system_prompt)
    assert "blender_render" in options.mcp_servers  # type: ignore[operator]
    assert "mcp__blender_render__stills" in options.allowed_tools
    assert not [t for t in options.allowed_tools if t.startswith("mcp__blender_render__blender")]
    assert (options.sandbox or {}).get("network", {}).get("allowedDomains") == []
    assert f"turns the script at {folder / script_id}" in script.prompts[0]


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_blender_build_starts_from_a_template_and_a_send_back_revises_it(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    def kit(world: Path) -> None:
        folder = world / "assets/blender_templates/kit"
        shutil.copytree(BLENDER_RENDER.parent / "blender_build/starter", folder)
        (folder / "TEMPLATE.md").write_text("Use kit.py.\n")

    def writes(name: str) -> Answer:  # the scripted CLI runs no tools, so the step writes
        return Answer(lambda options: (Path(options.cwd) / "shots" / name).write_text("# 1"))

    script = Script([writes("s02.py"), Say("v1")], [writes("s03.py"), Say("v2")])
    review = "  - gate: review\n    ask: Good?\n"
    client, run = build_run(
        tmp_path,
        SdkRuntime(transport=script.transport),
        "{base: kit, quality: final}",
        opened,
        after=review,
        calls="blender_build",
        before=kit,
    )
    assert run["status"] == "waiting_gate", run
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    assert f"Read {stage}/build/TEMPLATE.md first" in script.prompts[0]
    assert "quality `final`" in script.prompts[0]
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert call["template"]["name"] == "kit" and call["request"]["quality"] == "final"

    sent = client.post(
        f"/api/runs/{run['id']}/decision", json={"note": "Slower.", "send_back_to": "video"}
    )
    assert sent.status_code == 200, sent.text
    run = settled(client, run["id"], send_backs=1)
    assert run["status"] == "waiting_gate", run
    assert script.prompts[1].startswith("This is a revision, not a fresh start.")
    assert (stage / "build/shots/s02.py").is_file() and (stage / "build/shots/s03.py").is_file()
    assert (stage / "build.v1/shots/s02.py").is_file() and (stage / "build.v1.mp4").is_file()
    assert not (stage / "build.v1/shots/s03.py").exists()


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_rerun_of_a_build_whose_render_failed_revises_it_and_reuses_the_rendered_shots(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """Issue 418: every shot rendered, then the finish step failed, so the stage failed and the
    run waits at its Retro. A Rerun with a note revises the kept build in place, and the render
    reuses the shot it already made."""

    def finish(command: list[str] | None) -> Answer:  # the scripted CLI runs no tools
        def write(options: Any) -> None:
            spec = json.loads((Path(options.cwd) / "render.json").read_text())
            spec.pop("finish", None)
            (Path(options.cwd) / "render.json").write_text(
                json.dumps({**spec, **({"finish": command} if command else {})})
            )

        return Answer(write)

    skill = "---\nname: finish\ndescription: Test the finish.\n---\n\nRun it first.\n"
    proposal = {
        "skill": {
            "action": "new", "name": "finish", "content": skill, "summary": "Test the finish",
            "evidence": ["video: the finish step exited with 1"],
        },
        "workflow": None,
    }  # fmt: skip
    script = Script(
        [finish(["false"]), Say("v1")],
        [Answer(proposal)],
        [finish(None), Say("v2")],
        [Answer({"skill": None, "workflow": None})],
    )
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened,
        calls="blender_build", retro=True,
    )  # fmt: skip
    assert [s["status"] for s in run["stages"]] == ["done", "failed", "waiting"], run
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    shot = (stage / "shots/s01.mp4").stat().st_mtime_ns

    note = "Drop the finish step; join the shots."
    change = {"kind": "rerun", "stage": video_id, "payload": {"note": note}}
    proposed = client.post(f"/api/runs/{run['id']}/changes/propose", json=change).json()
    assert proposed["violations"] == [] and proposed["in_place"], proposed
    assert proposed["before"] == {"video": "failed", "retro": "waiting"}
    revision = proposed["revision"]
    applied = client.post(f"/api/runs/{run['id']}/changes", json={**change, "revision": revision})
    assert applied.status_code == 201, applied.text
    run = settled(client, run["id"], send_backs=1)
    assert run["status"] == "done", run

    revising = next(p for p in script.prompts if p.startswith("This is a revision"))
    assert note in revising and f"before you change anything: {stage}/build.v1." in revising
    assert "finish" in json.loads((stage / "build.v1/render.json").read_text())
    assert "finish" not in json.loads((stage / "build/render.json").read_text())
    assert (stage / "shots/s01.mp4").stat().st_mtime_ns == shot, "the shot was reused"
    call = client.get(f"/api/stages/{video_id}/call").json()
    assert (call["visit"], call["status"]) == (1, "done")


def until(client: TestClient, run_id: str, ready: Callable[[dict], bool]) -> dict:
    deadline = time.monotonic() + float(os.environ.get("SWARMIFY_BUILD_WAIT", 60))
    while not ready(run := client.get(f"/api/runs/{run_id}").json()):
        assert time.monotonic() < deadline, run
        time.sleep(0.05)
    return run


def _half(options: Any) -> None:
    """The builder's work before it is stopped: a shot the starter has not."""
    (Path(options.cwd) / "shots/s02.py").write_text("# half done")


def _builder(client: TestClient, run_id: str, stage_id: str) -> dict:
    agents = client.get(f"/api/runs/{run_id}/agents").json()
    [agent] = [a for a in agents if a["stage_id"] == stage_id and a["name"] == "builder"]
    return agent


USAGE = {"input_tokens": 1000}
"""Each scripted step costs something, so the cost hook asks whether the run was paused."""


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_build_paused_part_way_resumes_its_session_in_the_same_folder(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """Issue 423: paused mid-build, the builder stops at its next step; on resume the stage runs
    again for the same visit, its folder is not seeded again and the builder continues its
    session, told to carry on rather than given the brief again."""
    started: list[str] = []

    def pause(options: Any) -> None:
        _half(options)
        [client] = opened
        assert client.post(f"/api/runs/{started[0]}/pause").status_code == 200

    script = Script(
        [Answer(pause), Call("Bash", {"command": "ls"}), Say("never")],
        [Say("Finished the second shot.")],
        usage=USAGE,
    )
    runtime = SdkRuntime(transport=script.transport)
    client, run = build_run(
        tmp_path, runtime, "{brief: Loud.}", opened, calls="blender_build", wait=False
    )
    started.append(run["id"])
    run = until(client, run["id"], lambda r: r["status"] == "paused")
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    assert (stage / "build/shots/s02.py").read_text() == "# half done"
    assert client.get(f"/api/stages/{video_id}/call").status_code == 404  # no render yet

    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 200
    run = settled(client, run["id"])
    assert run["status"] == "done", run
    first, again = script.sessions
    assert first.resume is None and "Loud." in script.prompts[0]
    assert again.resume == _builder(client, run["id"], video_id)["session_id"]
    assert script.prompts[1] == build.CONTINUE
    assert (stage / "build/shots/s02.py").read_text() == "# half done", "not seeded again"


class Crash(BaseException):
    """The api process dying: DBOS records no outcome for the step, so the next start recovers
    the run and runs the stage again."""


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_build_cut_short_by_a_restart_resumes_its_session_in_the_same_folder(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """Issue 423: the api restarts mid-build (here the session's process dies with it, after its
    work); recovery runs the stage again for the same visit, which continues the session in the
    folder as it was."""
    script = Script([Answer(_half), Say("cut short")], [Say("Finished the second shot.")])
    sdk = SdkRuntime(transport=script.transport)

    class Restarts:
        async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
            end = await sdk.run(spec, hooks)
            if len(script.sessions) == 1:
                raise Crash()
            return end

    client, run = build_run(tmp_path, Restarts(), "{}", opened, calls="blender_build", wait=False)
    until(client, run["id"], lambda _: len(script.sessions) == 1)
    time.sleep(0.5)  # the crash leaves the run in flight
    client.__exit__(None, None, None)
    opened.remove(client)

    app = create_app(tmp_path / "data", poll_seconds=0.05, retro=False, runtime=Restarts())
    client = TestClient(app)
    client.__enter__()
    opened.append(client)
    run = settled(client, run["id"])
    assert run["status"] == "done", run
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    first, again = script.sessions
    assert first.resume is None
    assert again.resume == _builder(client, run["id"], video_id)["session_id"]
    assert script.prompts[1] == build.CONTINUE
    assert (stage / "build/shots/s02.py").read_text() == "# half done", "not seeded again"


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_build_session_that_died_is_continued_in_the_same_folder(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    script = Script([Answer(_half), Die()], [Say("Finished the second shot.")])
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened, calls="blender_build"
    )
    assert run["status"] == "done", run
    video_id = run["stages"][1]["id"]
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    assert script.sessions[1].resume and script.prompts[1] == build.CONTINUE
    assert (stage / "build/shots/s02.py").read_text() == "# half done"


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_pause_during_the_render_lands_after_it_and_resume_goes_on(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """Issue 423: the render, a long job, is not stopped midway; the pause lands when it ends,
    the run log says so, and resume goes on from the next stage without building or rendering
    again."""

    def slow(options: Any) -> None:
        (Path(options.cwd) / "shots/s01.py").write_text("# slow\n# frames: 6\n")

    script = Script([Answer(slow), Say("Six slow frames.")])
    after = "  - gate: review\n    ask: Good?\n"
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened,
        calls="blender_build", after=after, wait=False,
    )  # fmt: skip
    run = until(client, run["id"], lambda r: r["stages"][1]["status"] == "running")
    video_id = run["stages"][1]["id"]
    deadline = time.monotonic() + 30
    while client.get(f"/api/stages/{video_id}/call").status_code != 200:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.post(f"/api/runs/{run['id']}/pause").status_code == 200
    run = until(client, run["id"], lambda r: r["status"] == "paused")
    assert [s["status"] for s in run["stages"][:2]] == ["done", "done"], run
    log = client.get(f"/api/runs/{run['id']}/stages/{video_id}/log").json()
    assert any("paused during the render" in e["detail"] for e in log)

    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 200
    run = settled(client, run["id"])
    assert run["status"] == "waiting_gate", run
    assert len(script.sessions) == 1
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    calls = (stage / "frames/calls.jsonl").read_text().splitlines()
    assert [json.loads(c)["event"] for c in calls] == ["start", "end"], "rendered once"


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_revision_paused_part_way_keeps_its_edits_and_its_kept_version(
    tmp_path: Path, fake_blender: None, opened: list[TestClient]
) -> None:
    """A send-back's visit paused mid-revision: on resume the half-done revision stays (it is not
    copied again from build.v1) and the builder continues the revision's session, not the first
    visit's."""
    started: list[str] = []

    def pause(options: Any) -> None:
        _half(options)
        [client] = opened
        assert client.post(f"/api/runs/{started[0]}/pause").status_code == 200

    script = Script(
        [Say("v1")],
        [Answer(pause), Call("Bash", {"command": "ls"}), Say("never")],
        [Say("v2")],
        usage=USAGE,
    )
    review = "  - gate: review\n    ask: Good?\n"
    client, run = build_run(
        tmp_path, SdkRuntime(transport=script.transport), "{}", opened,
        calls="blender_build", after=review,
    )  # fmt: skip
    started.append(run["id"])
    video_id = run["stages"][1]["id"]
    first = _builder(client, run["id"], video_id)["session_id"]
    sent = client.post(
        f"/api/runs/{run['id']}/decision", json={"note": "More.", "send_back_to": "video"}
    )
    assert sent.status_code == 200, sent.text
    until(client, run["id"], lambda r: r["status"] == "paused")
    revising = _builder(client, run["id"], video_id)["session_id"]
    assert revising != first

    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 200
    run = settled(client, run["id"], send_backs=1)
    assert run["status"] == "waiting_gate", run
    assert [o.resume for o in script.sessions] == [None, None, revising]
    assert (
        script.prompts[1].startswith("This is a revision") and script.prompts[2] == build.CONTINUE
    )
    stage = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"] / video_id
    assert (stage / "build/shots/s02.py").read_text() == "# half done"
    assert not (stage / "build.v1/shots/s02.py").exists() and (stage / "build.v1.mp4").is_file()
