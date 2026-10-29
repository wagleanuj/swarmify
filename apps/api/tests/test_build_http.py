"""A script stage then a `remotion_build` stage, end to end through the API: real DBOS on SQLite,
the real MCP transport and render wrapper, the Agent SDK client with a scripted CLI as the
builder, and a fake Remotion CLI on PATH rendering inside the OS sandbox. One real build, on the
subscription and the real Remotion CLI, runs when you opt in with SWARMIFY_REAL_BUILD=1."""

import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_swarm import AgentRuntime, SdkRuntime
from swarmify_swarm.fake import Answer, Call, Say, Script

HERE = Path(__file__).parent
FAKE_NPX = HERE.parents[2] / "packages/connectors/tests/fake_npx.py"
SANDBOX = shutil.which("sandbox-exec") or shutil.which("bwrap")


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(f"#!{sys.executable}\n" + FAKE_NPX.read_text())
    shell = bin_dir / "chrome-headless-shell"
    shell.write_text("#!/bin/sh\n")
    for tool in (npx, shell):
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def opened() -> Iterator[list[TestClient]]:
    """Every app a test starts, shut down after it whatever happened, so DBOS stops."""
    clients: list[TestClient] = []
    yield clients
    for client in clients:
        client.__exit__(None, None, None)


def _installs(options: Any) -> None:
    """What `npm install` leaves, done by the scripted builder."""
    (Path(options.cwd) / "node_modules").mkdir()


def build_run(
    tmp_path: Path,
    runtime: AgentRuntime,
    params: str,
    opened: list[TestClient],
    inputs: str = "input: script.md",
    after: str = "",
) -> tuple[TestClient, dict]:
    """`inputs` is the build stage's input line; a notes stage writing notes.md runs before it
    when it names notes.md. `after` is YAML for stages after it; the run is returned once it
    ends or waits at a gate."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / ".env").write_text("UPLOAD_POST_API_KEY=k3y\n")
    (data / ".env").chmod(0o600)
    app = create_app(data, poll_seconds=0.05, retro=False, runtime=runtime, extract=fake_extract)
    client = TestClient(app)
    client.__enter__()
    opened.append(client)
    w = client.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
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
    notes = "  - connector: notes\n    calls: script_writer\n    output: notes.md\n"
    yaml = f"""name: Script then build
stages:
  - connector: script
    calls: script_writer
    output: script.md
{notes if "notes.md" in inputs else ""}  - connector: video
    calls: remotion_build
    {inputs}
    params: {params}
{after}"""
    workflow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
    assert workflow.status_code == 201, workflow.text
    run = client.post(f"/api/workflows/{workflow.json()['workflow_id']}/runs", json={}).json()
    return client, settled(client, run["id"])


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
