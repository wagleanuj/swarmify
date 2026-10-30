"""The Remotion render connector: the built-in wrapper started over stdio MCP, rendering with a
fake Remotion CLI on PATH. `SWARMIFY_REMOTION_PROJECT` opts in to one real render."""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from swarmify_connectors import (
    BUILTINS,
    ConnectorError,
    Connectors,
    McpTransport,
    SqliteStorage,
    remotion,
)
from swarmify_connectors.credentials import Credentials
from swarmify_connectors.remotion import alive, progress
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect

W = "world_1"


@pytest.fixture
def real(tmp_path: Path, db: Path) -> Connectors:
    """The service with the real MCP transport, polling fast."""
    return Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)


def stage(tmp_path: Path, project: Path, composition: str = "Short", **params: Any) -> dict:
    script = tmp_path / "stages/1-script/artifacts/files/script.md"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# Why the chase works\n\nHook, then the reveal.\n")
    return {
        "calls": "remotion_render",
        "input": "script.md",
        "output": "short.mp4",
        "params": {
            "project": str(project),
            "composition": composition,
            "input_path": str(script),
            "output_path": str(tmp_path / "stages/2-render/artifacts/files/short.mp4"),
            "frontmatter": {"world": W, "run": "run_1", "stage": "render"},
            **params,
        },
    }


def test_progress_reads_the_cli_lines() -> None:
    assert progress("") == (0.0, "starting the render")
    assert progress("Bundling 40%\nRendered 15/30, time remaining: 1s\n") == (
        0.45,
        "rendered 15/30 frames",
    )
    assert progress("Rendered 30/30\nEncoded 24/30\n") == (0.98, "encoded 24/30 frames")


def test_renders_the_script_into_a_video_with_a_sidecar(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, frames=10)  # YAML hands an int for a single frame
    call = real.run_stage(W, "run_1", "stage_2", config)
    assert call.status == "done" and call.progress == 1.0 and call.polls >= 1
    video = Path(config["params"]["output_path"])
    assert call.response is not None and call.response["output"] == str(video)
    rendered = json.loads(video.read_text())
    assert rendered["composition"] == "Short" and rendered["entry"] == "src/index.ts"
    assert rendered["script"].startswith("# Why the chase works")
    assert rendered["scriptPath"] == config["params"]["input_path"]

    sidecar = video.with_name("short.mp4.md")
    assert call.response["sidecar"] == str(sidecar)
    text = sidecar.read_text()
    head = text.split("---\n")[1]
    assert 'type: "video"' in head and 'world: "world_1"' in head and 'run: "run_1"' in head
    assert 'stage: "render"' in head and "created: " in head
    assert "from the script `script.md` with composition `Short`" in text


def test_a_headless_shell_on_path_is_the_browser_remotion_uses(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """The Docker image puts Chrome Headless Shell on PATH (README, Docker); a Mac has none and
    Remotion finds or downloads its own."""
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert json.loads(Path(config["params"]["output_path"]).read_text())["browser"] == ""

    shell = tmp_path / "bin" / "chrome-headless-shell"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    real.run_stage(W, "run_1", "stage_3", config)
    assert json.loads(Path(config["params"]["output_path"]).read_text())["browser"] == str(shell)


def test_the_render_concurrency_setting_caps_the_tabs_rendering_at_once(
    tmp_path: Path, real: Connectors, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, Remotion picks; the Docker image sets 2, since a 3D scene in software runs out of
    memory with more (spike 2026-10-30). A value that is not a plain one is refused."""
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert json.loads(Path(config["params"]["output_path"]).read_text())["concurrency"] == ""

    monkeypatch.setenv(remotion.CONCURRENCY, "2")
    real.run_stage(W, "run_1", "stage_3", config)
    assert json.loads(Path(config["params"]["output_path"]).read_text())["concurrency"] == "2"

    monkeypatch.setenv(remotion.CONCURRENCY, "--gl=angle")
    with pytest.raises(ConnectorError, match="concurrency must be a plain value"):
        real.run_stage(W, "run_1", "stage_4", config)


def test_a_relative_project_is_a_template_under_the_data_directory(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """`real` keeps its credentials in tmp_path, so tmp_path is the data directory. A symlink
    there is followed, so a template can stay in its own folder."""
    (tmp_path / "remotion").mkdir()
    (tmp_path / "remotion" / "shorts").symlink_to(fake_npx)
    config = stage(tmp_path, Path("shorts"))
    assert real.run_stage(W, "run_1", "stage_2", config).status == "done"
    assert json.loads(Path(config["params"]["output_path"]).read_text())["composition"] == "Short"


@pytest.mark.parametrize(("lockfile", "command"), [(True, "ci"), (False, "install")])
def test_a_template_without_node_modules_installs_them_once_before_rendering(
    tmp_path: Path, real: Connectors, fake_npx: Path, lockfile: bool, command: str
) -> None:
    (fake_npx / "node_modules").rmdir()
    if lockfile:
        (fake_npx / "package-lock.json").write_text("{}")
    config = stage(tmp_path, fake_npx)
    log = Path(config["params"]["output_path"]).with_name(".short.mp4.log")
    real.run_stage(W, "run_1", "stage_2", config)
    assert f"fake npm {command}" in log.read_text() and "Encoded 3/3" in log.read_text()
    assert (fake_npx / "node_modules").is_dir()
    real.run_stage(W, "run_1", "stage_3", config)  # fake npm fails if node_modules exists
    assert "fake npm" not in log.read_text() and "Encoded 3/3" in log.read_text()


def test_the_sidecar_records_the_template_commit_of_a_git_checkout(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx)
    sidecar = Path(config["params"]["output_path"]).with_name("short.mp4.md")
    real.run_stage(W, "run_1", "stage_2", config)
    assert "commit: " not in sidecar.read_text()

    git = ["git", "-C", str(fake_npx), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "template"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    real.run_stage(W, "run_1", "stage_3", config)
    assert f'commit: "{sha.stdout.strip()}"' in sidecar.read_text()


def test_progress_reaches_the_call_while_it_renders(
    tmp_path: Path, db: Path, real: Connectors, fake_npx: Path
) -> None:
    real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Slow"))
    with closing(connect(db)) as conn:
        seen = [e.payload for e in read_after(conn, W, 0) if e.type == "connector_call.updated"]
    messages = [p["message"] for p in seen if p["status"] == "polling"]
    assert any(m and m.startswith("rendered ") for m in messages), messages
    values = [p["progress"] for p in seen if p["progress"] is not None]
    assert values == sorted(values) and values[-1] == 1.0


def test_a_failed_render_fails_the_stage_with_the_cli_output(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    with pytest.raises(ConnectorError) as raised:
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Broken"))
    assert "remotion render exited with 1" in str(raised.value)
    assert "No composition with the ID Broken found." in str(raised.value)
    call = real.call_for("stage_2")
    assert call is not None and call.status == "failed"
    assert "No composition with the ID Broken" in (call.error or "")


def test_a_render_that_writes_nothing_fails(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    with pytest.raises(ConnectorError, match="wrote no video"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Empty"))


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"input_path": "/nope/script.md"}, "the script /nope/script.md does not exist"),
        ({"project": "/nope"}, "the Remotion project /nope does not exist"),
        ({"input_path": ""}, "the render stage needs an `input`"),
        ({"composition": "--browser-executable=/bin/sh"}, "composition must be a plain value"),
    ],
)
def test_a_bad_request_is_refused_before_rendering(
    tmp_path: Path, real: Connectors, fake_npx: Path, over: dict, problem: str
) -> None:
    config = stage(tmp_path, fake_npx)
    config["params"].update(over)
    with pytest.raises(ConnectorError, match=problem):
        real.run_stage(W, "run_1", "stage_2", config)


def test_a_repeated_call_attaches_to_the_render_instead_of_starting_another(
    tmp_path: Path, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, "Slow")
    request = {**config["params"], "idempotency_key": "stage_2"}
    connector = next(c for c in BUILTINS if c.name == "remotion_render")
    transport = McpTransport()
    first = transport.call(connector, "remotion_render", request, {})
    again = transport.call(connector, "remotion_render", request, {})
    assert first["job_id"] == again["job_id"]
    assert (first["message"], again["message"]) == ("render started", "render already started")
    job = Path(first["job_id"])
    deadline = time.monotonic() + 10
    while json.loads(job.read_text())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    done = transport.call(connector, "remotion_render_status", {"job_id": str(job)}, {})
    assert done["status"] == "done"
    assert transport.call(connector, "remotion_render", request, {})["message"] == (
        "render already started"
    )


@pytest.mark.skipif(
    not os.environ.get("SWARMIFY_REMOTION_PROJECT"),
    reason="set SWARMIFY_REMOTION_PROJECT (and SWARMIFY_REMOTION_COMPOSITION) for a real render",
)
def test_a_real_render(tmp_path: Path, real: Connectors) -> None:
    project = Path(os.environ["SWARMIFY_REMOTION_PROJECT"])
    composition = os.environ.get("SWARMIFY_REMOTION_COMPOSITION", "Main")
    config = stage(tmp_path, project, composition, frames="0-9")
    call = real.run_stage(W, "run_1", "stage_2", config)
    video = Path(config["params"]["output_path"])
    assert call.status == "done" and video.stat().st_size > 1000
    assert video.read_bytes()[4:8] == b"ftyp"
    assert video.with_name("short.mp4.md").exists()


def test_pid_zero_is_never_a_live_render() -> None:
    assert not alive(0) and alive(os.getpid())


def test_a_saved_render_stage_names_its_project_and_composition(
    tmp_path: Path, connectors: Connectors
) -> None:
    render = {"calls": "remotion_render", "input": "script.md", "output": "short.mp4"}
    assert connectors.stage_problems(W, render) == [
        "remotion_render needs params.project, the Remotion project folder",
        "remotion_render needs params.composition, the composition ID",
    ]
    gone = tmp_path / "nope"
    assert connectors.stage_problems(W, {**render, "params": {"project": str(gone)}}) == [
        f"the Remotion project {gone} does not exist",
        "remotion_render needs params.composition, the composition ID",
    ]
    ok = {"project": str(tmp_path), "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": ok}) == []
    named = {"project": "shorts", "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": named}) == [
        f"the Remotion project shorts does not exist in {tmp_path / 'remotion'}"
    ]
    (tmp_path / "remotion" / "shorts").mkdir(parents=True)
    assert connectors.stage_problems(W, {**render, "params": named}) == []


def _built(tmp_path: Path, fake_npx: Path, db: Path) -> tuple[Connectors, dict, Path]:
    """A render stage of a project a build stage wrote: under the data directory's worlds. The
    data directory is `data`, so the fake CLI in tmp_path/bin stays readable."""
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cret\n")
    (data / ".env").chmod(0o600)
    connectors = Connectors(SqliteStorage(db), McpTransport(), Credentials(data / ".env"), 0.05)
    project = data / "worlds/world_1/runs/run_1/stage_2/build"
    project.parent.mkdir(parents=True)
    fake_npx.rename(project)
    shell = tmp_path / "bin" / "chrome-headless-shell"  # as in the image: nothing to download
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    config = stage(tmp_path, project, "Snoop")
    config["params"]["output_path"] = str(project.parent / "artifacts/video.mp4")
    return connectors, config, project


@pytest.mark.skipif(
    not (shutil.which("sandbox-exec") or shutil.which("bwrap")), reason="no OS sandbox here"
)
def test_a_built_project_renders_in_a_sandbox_it_cannot_leave(
    tmp_path: Path, db: Path, fake_npx: Path
) -> None:
    connectors, config, project = _built(tmp_path, fake_npx, db)
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    rendered = json.loads(Path(config["params"]["output_path"]).read_text())
    snooped = rendered["snooped"]
    assert set(snooped) == {"read_keys", "write_outside", "network"}
    assert all(v.startswith("refused") for v in snooped.values()), snooped
    assert not (tmp_path / "data/planted.txt").exists()
    assert rendered["browser"] == str(tmp_path / "bin" / "chrome-headless-shell")


def test_a_built_project_is_never_installed_outside_the_sandbox(
    tmp_path: Path, db: Path, fake_npx: Path
) -> None:
    """Its package.json is the agent's: `npm install` would run the agent's scripts."""
    connectors, config, project = _built(tmp_path, fake_npx, db)
    (project / "node_modules").rmdir()
    with pytest.raises(ConnectorError, match="the build left no node_modules"):
        connectors.run_stage(W, "run_1", "stage_2", config)
    assert not (project / "node_modules").exists()


@pytest.fixture
def port_3000() -> Iterator[None]:
    """Something else on Remotion's default port, as an open-webui container was."""
    try:
        server = ThreadingHTTPServer(("localhost", 3000), SimpleHTTPRequestHandler)
    except OSError:  # already held, which is the point
        yield
        return
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("built", [False, True])
def test_a_render_uses_a_free_port_not_one_another_server_holds(
    tmp_path: Path, db: Path, real: Connectors, fake_npx: Path, port_3000: None, built: bool
) -> None:
    """Template renders and remotion_build's sandboxed renders alike."""
    if built and not (shutil.which("sandbox-exec") or shutil.which("bwrap")):
        pytest.skip("no OS sandbox here")
    connectors, config = (
        (_built(tmp_path, fake_npx, db)[:2]) if built else (real, stage(tmp_path, fake_npx))
    )
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"


def test_with_no_sandbox_a_built_project_does_not_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remotion.shutil, "which", lambda _: None)
    with pytest.raises(OSError, match="install bubblewrap"):
        remotion.sandboxed(["npx"], tmp_path / "p", tmp_path / "o/v.mp4", tmp_path)


def test_a_build_is_seeded_from_the_starter_or_a_template_by_name(tmp_path: Path) -> None:
    folder = tmp_path / "worlds/w/runs/r/s/build"
    remotion.seed(folder, None, tmp_path)
    assert "registerRoot" in (folder / "src/index.ts").read_text()
    assert 'id="Main"' in (folder / "src/Root.tsx").read_text()
    assert (folder / "package-lock.json").is_file()

    shorts = tmp_path / "remotion/shorts"
    (shorts / "node_modules/x").mkdir(parents=True)
    (shorts / "src").mkdir()
    (shorts / "src/index.ts").write_text("// shorts")
    remotion.seed(folder, "shorts", tmp_path)  # a fresh folder each time
    assert (folder / "src/index.ts").read_text() == "// shorts"
    assert not (folder / "node_modules").exists() and not (folder / "src/Root.tsx").exists()
    for base in ("../shorts", str(shorts), "nope"):
        with pytest.raises(ValueError):
            remotion.seed(folder, base, tmp_path)


def test_a_saved_build_stage_names_its_script_and_an_existing_template(
    tmp_path: Path, connectors: Connectors
) -> None:
    build = {"calls": "remotion_build", "output": "video.mp4"}
    assert connectors.stage_problems(W, build) == ["remotion_build needs an input, the script"]
    assert connectors.stage_problems(W, {**build, "inputs": ["script.md", "t.json"]}) == []
    build["input"] = "script.md"
    assert connectors.stage_problems(W, build) == []
    assert connectors.stage_problems(W, {**build, "params": {"base": "/etc"}}) == [
        f"base must be a template name under {tmp_path / 'remotion'}, not a path"
    ]
    assert connectors.stage_problems(W, {**build, "params": {"base": "shorts"}}) == [
        f"the Remotion template shorts does not exist in {tmp_path / 'remotion'}"
    ]


def test_secret_names_cover_the_file_and_every_declared_key(
    tmp_path: Path, connectors: Connectors
) -> None:
    (tmp_path / ".env").write_text("OTHER_KEY=x\n")
    (tmp_path / ".env").chmod(0o600)
    names = connectors.secret_names(W)
    assert {"OTHER_KEY", "YOUTUBE_REFRESH_TOKEN"} <= set(names)
