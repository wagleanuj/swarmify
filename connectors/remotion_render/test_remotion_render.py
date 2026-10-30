"""The Remotion render connector: the package installed through the real install path (with the
suite's fake `npm` and `npx`), given to a world as a default, started over stdio MCP and rendering
with a fake Remotion CLI on PATH (`fake_npx.py`). `SWARMIFY_REMOTION_PROJECT` opts in to one real
render."""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
import remotion_render as rr

from swarmify_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
W = "w"
Given = Callable[..., Connectors]


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One install for the module, as Swarmify does at startup: copied to the packages folder,
    `uv sync`ed there, then the (fake) npm and Remotion browser steps."""
    data = tmp_path_factory.mktemp("data")
    defaults = registry(data, FakeTransport()).install_defaults()
    assert defaults["remotion_render"] == "1.0.0"
    return data / "packages"


@pytest.fixture
def given(installed: Path, tmp_path: Path) -> Given:
    """A registry with data directory `data` (tmp_path by default) sharing that install, the
    package given to world `w` as a default."""

    def make(transport: Any, data: Path = tmp_path) -> Connectors:
        data.mkdir(exist_ok=True)
        (data / "packages").symlink_to(installed)
        connectors = registry(data, transport)
        connectors.install_defaults()  # installed already: only remembered
        connectors.offer_defaults(W)
        return connectors

    return make


@pytest.fixture
def real(given: Given) -> Connectors:
    return given(McpTransport())


@pytest.fixture
def fake_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake Remotion CLI first on PATH, as `npx`, and a fake `npm` that prints its command
    and makes `node_modules`. Returns a ready Remotion project, its dependencies installed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(f"#!{sys.executable}\n" + (HERE / "fake_npx.py").read_text())
    npm = bin_dir / "npm"
    npm.write_text(
        f"#!{sys.executable}\nimport os, sys\nprint('fake npm', *sys.argv[1:])\n"
        "os.mkdir('node_modules')\n"
    )
    for tool in (npx, npm):
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    project = tmp_path / "video"
    (project / "src").mkdir(parents=True)
    (project / "node_modules").mkdir()
    return project


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


def rendered(config: dict) -> dict:
    return json.loads(Path(config["params"]["output_path"]).read_text())


# The package


def test_the_manifest_passes_and_declares_the_check_capability() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["capabilities"]) == ("remotion_render", "stage", ["check"])
    assert m["params"]["required"] == ["project", "composition"]


def test_install_puts_its_python_the_remotion_cli_and_its_browser_beside_it(
    installed: Path,
) -> None:
    folder = installed / "remotion_render" / "1.0.0"
    assert (folder / ".venv" / "bin" / "python").is_file()
    assert (folder / "fake-install.log").read_text().splitlines() == [
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npx --no remotion browser ensure",
    ]


def test_its_settings_reach_the_process_the_data_folder_from_the_data_directory(
    given: Given, tmp_path: Path
) -> None:
    connectors = given(FakeTransport())
    render = connectors.find(W, "remotion_render")
    assert render is not None and render.version == "1.0.0"
    assert connectors._env(render) == {"REMOTION_DATA_DIR": str(tmp_path)}
    connectors.save_setup(W, "remotion_render", {"data_folder": "elsewhere"}, {"concurrency": "2"})
    assert connectors._env(render) == {
        "REMOTION_DATA_DIR": str(tmp_path / "elsewhere"),
        "SWARMIFY_RENDER_CONCURRENCY": "2",
    }
    connectors.save_setup(W, "remotion_render", {"data_folder": "/srv/swarmify"}, {})
    assert connectors._env(render)["REMOTION_DATA_DIR"] == "/srv/swarmify"
    with pytest.raises(ValueError, match="Render concurrency: does not match"):
        connectors.save_setup(W, "remotion_render", {}, {"concurrency": "--gl=angle"})


# The workflow check, through the package's `check` tool


def test_a_saved_render_stage_names_its_project_and_composition(
    given: Given, tmp_path: Path
) -> None:
    connectors = given(McpTransport())
    render = {"calls": "remotion_render", "input": "script.md", "output": "short.mp4"}
    assert connectors.stage_problems(W, render) == [
        "remotion_render needs params.project, the Remotion project folder",
        "remotion_render needs params.composition, the composition ID",
    ]
    gone = tmp_path / "nope"
    assert connectors.stage_problems(W, {**render, "params": {"project": str(gone)}}) == [
        "remotion_render needs params.composition, the composition ID",  # its params schema
        f"the Remotion project {gone} does not exist",  # its check
    ]
    ok = {"project": str(tmp_path), "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": ok}) == []
    named = {"project": "shorts", "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": named}) == [
        f"the Remotion project shorts does not exist in {tmp_path / 'remotion'}"
    ]
    (tmp_path / "remotion" / "shorts").mkdir(parents=True)
    assert connectors.stage_problems(W, {**render, "params": named}) == []
    assert connectors.stage_problems(W, {"calls": "remotion_render", "params": named}) == [
        "remotion_render needs an input, the script",
        "remotion_render needs an output, the video",
    ]


def test_a_check_that_cannot_run_is_a_problem(given: Given) -> None:
    connectors = given(FakeTransport(outputs={"check": [ConnectorError("it crashed")]}))
    params = {"project": "shorts", "composition": "Short"}
    config = {"calls": "remotion_render", "input": "s.md", "output": "v.mp4", "params": params}
    assert connectors.stage_problems(W, config) == ["remotion_render check failed: it crashed"]


# Rendering over stdio


def test_progress_reads_the_cli_lines() -> None:
    assert rr.progress("") == (0.0, "starting the render")
    assert rr.progress("Bundling 40%\nRendered 15/30, time remaining: 1s\n") == (
        0.45,
        "rendered 15/30 frames",
    )
    assert rr.progress("Rendered 30/30\nEncoded 24/30\n") == (0.98, "encoded 24/30 frames")


def test_renders_the_script_into_a_video_with_a_sidecar(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, frames=10)  # YAML hands an int for a single frame
    call = real.run_stage(W, "run_1", "stage_2", config)
    assert call.status == "done" and call.progress == 1.0 and call.polls >= 1
    video = Path(config["params"]["output_path"])
    assert call.response is not None and call.response["output"] == str(video)
    out = rendered(config)
    assert out["composition"] == "Short" and out["entry"] == "src/index.ts"
    assert out["script"].startswith("# Why the chase works")
    assert out["scriptPath"] == config["params"]["input_path"]

    sidecar = video.with_name("short.mp4.md")
    assert call.response["sidecar"] == str(sidecar)
    text = sidecar.read_text()
    head = text.split("---\n")[1]
    assert 'type: "video"' in head and 'world: "w"' in head and 'run: "run_1"' in head
    assert 'stage: "render"' in head and "created: " in head
    assert "from the script `script.md` with composition `Short`" in text


@pytest.fixture
def own_browser(installed: Path) -> Iterator[Path]:
    """What `remotion browser ensure` leaves beside the installed package, for one test."""
    browsers = installed / "remotion_render" / "1.0.0" / "node_modules" / ".remotion"
    shell = browsers / "chrome-headless-shell" / "linux64" / "chrome-headless-shell"
    shell.parent.mkdir(parents=True)
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    yield shell
    shutil.rmtree(browsers)


def test_its_own_browser_is_the_one_remotion_uses_else_one_on_path(
    tmp_path: Path, real: Connectors, fake_npx: Path, request: pytest.FixtureRequest
) -> None:
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert rendered(config)["browser"] == ""  # none: Remotion finds or downloads its own

    shell = tmp_path / "bin" / "chrome-headless-shell"  # as Swarmify's image has
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    real.run_stage(W, "run_1", "stage_3", config)
    assert rendered(config)["browser"] == str(shell)

    own = request.getfixturevalue("own_browser")
    real.run_stage(W, "run_1", "stage_4", config)
    assert Path(rendered(config)["browser"]).resolve() == own.resolve()


def test_the_render_concurrency_setting_caps_the_tabs_rendering_at_once(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """Unset, Remotion picks; Docker sets 2, since a 3D scene in software runs out of memory
    with more (spike 2026-10-30)."""
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert rendered(config)["concurrency"] == ""

    real.save_setup(W, "remotion_render", {"concurrency": "2"}, {})
    real.run_stage(W, "run_1", "stage_3", config)
    assert rendered(config)["concurrency"] == "2"


def test_a_relative_project_is_a_template_under_the_data_folder(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """The data folder is the data directory, tmp_path. A symlink there is followed, so a
    template can stay in its own folder."""
    (tmp_path / "remotion").mkdir()
    (tmp_path / "remotion" / "shorts").symlink_to(fake_npx)
    config = stage(tmp_path, Path("shorts"))
    assert real.run_stage(W, "run_1", "stage_2", config).status == "done"
    assert rendered(config)["composition"] == "Short"


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
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Slow"))
    with closing(connect(tmp_path / "swarmify.db")) as conn:
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
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, "Slow")
    request = {**config["params"], "idempotency_key": "stage_2"}
    connector = real.find(W, "remotion_render")
    assert connector is not None
    env = real._env(connector)
    transport = McpTransport()
    first = transport.call(connector, "remotion_render", request, env)
    again = transport.call(connector, "remotion_render", request, env)
    assert first["job_id"] == again["job_id"]
    assert (first["message"], again["message"]) == ("render started", "render already started")
    job = Path(first["job_id"])
    deadline = time.monotonic() + 10
    while json.loads(job.read_text())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    done = transport.call(connector, "remotion_render_status", {"job_id": str(job)}, env)
    assert done["status"] == "done"
    assert transport.call(connector, "remotion_render", request, env)["message"] == (
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
    assert not rr.alive(0) and rr.alive(os.getpid())


# A project an agent wrote (a remotion_build stage) renders sandboxed


def _built(tmp_path: Path, fake_npx: Path, given: Given) -> tuple[Connectors, dict, Path]:
    """A render stage of a project a build stage wrote: under the data directory's worlds. The
    data directory is `data`, so the fake CLI in tmp_path/bin stays readable."""
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cret\n")
    (data / ".env").chmod(0o600)
    connectors = given(McpTransport(), data)
    project = data / "worlds/w/runs/run_1/stage_2/build"
    project.parent.mkdir(parents=True)
    fake_npx.rename(project)
    shell = tmp_path / "bin" / "chrome-headless-shell"  # as in the image: nothing to download
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    config = stage(tmp_path, project, "Snoop")
    config["params"]["output_path"] = str(project.parent / "artifacts/video.mp4")
    return connectors, config, project


SANDBOX = shutil.which("sandbox-exec") or shutil.which("bwrap")


@pytest.mark.skipif(not SANDBOX, reason="no OS sandbox here")
def test_a_built_project_renders_in_a_sandbox_it_cannot_leave(
    tmp_path: Path, given: Given, fake_npx: Path
) -> None:
    connectors, config, project = _built(tmp_path, fake_npx, given)
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    out = rendered(config)
    snooped = out["snooped"]
    assert set(snooped) == {"read_keys", "write_outside", "network"}
    assert all(v.startswith("refused") for v in snooped.values()), snooped
    assert not (tmp_path / "data/planted.txt").exists()
    assert out["browser"] == str(tmp_path / "bin" / "chrome-headless-shell")


def test_a_built_project_is_never_installed_outside_the_sandbox(
    tmp_path: Path, given: Given, fake_npx: Path
) -> None:
    """Its package.json is the agent's: `npm install` would run the agent's scripts."""
    connectors, config, project = _built(tmp_path, fake_npx, given)
    (project / "node_modules").rmdir()
    with pytest.raises(ConnectorError, match="the build left no node_modules"):
        connectors.run_stage(W, "run_1", "stage_2", config)
    assert not (project / "node_modules").exists()


def test_a_built_project_needs_a_browser_it_cannot_download_in_the_sandbox(
    tmp_path: Path, given: Given, fake_npx: Path
) -> None:
    connectors, config, _ = _built(tmp_path, fake_npx, given)
    (tmp_path / "bin" / "chrome-headless-shell").unlink()
    if shutil.which("chrome-headless-shell"):
        pytest.skip("a Chrome Headless Shell on this PATH")
    with pytest.raises(ConnectorError, match="no Chrome Headless Shell: reinstall"):
        connectors.run_stage(W, "run_1", "stage_2", config)


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
    tmp_path: Path, given: Given, fake_npx: Path, port_3000: None, built: bool
) -> None:
    """Template renders and remotion_build's sandboxed renders alike."""
    if built and not SANDBOX:
        pytest.skip("no OS sandbox here")
    connectors, config = (
        _built(tmp_path, fake_npx, given)[:2]
        if built
        else (given(McpTransport()), stage(tmp_path, fake_npx))
    )
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"


def test_with_no_sandbox_a_built_project_does_not_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rr.shutil, "which", lambda _: None)
    with pytest.raises(OSError, match="install bubblewrap"):
        rr.sandboxed(["npx"], tmp_path / "p", tmp_path / "o/v.mp4", tmp_path)
