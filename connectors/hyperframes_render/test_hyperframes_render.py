"""The HyperFrames render connector: the package installed through the real install path (with
the suite's fake `npm` and `npx`), enabled for a world, started over stdio MCP in the connector
sandbox and rendering with a fake HyperFrames CLI as the project's own (`fake_hyperframes.py`),
its project a build folder of the run. `SWARMIFY_REAL_BUILD=1` with
`SWARMIFY_HYPERFRAMES_PROJECT` opts in to one real install and render."""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import hyperframes_render as hr
import pytest

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
TOOLS = ("chrome-headless-shell", "ffmpeg", "ffprobe")
"""What a render needs besides the project's CLI, which the suite puts on PATH."""


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
    `uv sync`ed there, then the (fake) npm, ffmpeg and HyperFrames browser steps. It is
    installed but not a default: Remotion stays the engine a world has until it asks."""
    data = tmp_path_factory.mktemp("data")
    defaults = registry(data, FakeTransport()).install_defaults()
    assert "hyperframes_render" not in defaults
    assert (data / "packages" / "hyperframes_render" / "1.0.0").is_dir()
    return data / "packages"


@pytest.fixture
def given(installed: Path, tmp_path: Path) -> Given:
    """A registry with data directory `data` (tmp_path by default) sharing that install, the
    package enabled for world `w` by its operator."""

    def make(transport: Any, data: Path = tmp_path) -> Connectors:
        data.mkdir(exist_ok=True)
        (data / "packages").symlink_to(installed)
        connectors = registry(data, transport)
        connectors.install_defaults()  # installed already: only remembered
        connectors.offer_defaults(W)
        connectors.enable(W, "hyperframes_render", "1.0.0")
        return connectors

    return make


@pytest.fixture
def real(given: Given) -> Connectors:
    return given(McpTransport())


RUN = Path("worlds") / W / "runs" / "run_1"
"""Where the run's files are under the data directory: all a render may read of it."""


@pytest.fixture
def path_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Chrome Headless Shell, ffmpeg and ffprobe on PATH, as an operator running natively has,
    and no other."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in TOOLS:
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    kept = [d for d in os.environ["PATH"].split(os.pathsep) if not _has_tool(Path(d))]
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), *kept]))
    return bin_dir


def _has_tool(folder: Path) -> bool:
    return any((folder / name).exists() for name in (*TOOLS, "chromium", "google-chrome"))


@pytest.fixture
def project(tmp_path: Path, path_tools: Path) -> Path:
    """A HyperFrames project an earlier stage of the run wrote, its dependencies installed: a
    fake HyperFrames CLI (`fake_hyperframes.py`) as its own."""
    folder = tmp_path / RUN / "stage_1" / "build"
    folder.mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html>\n")
    cli = folder / hr.CLI
    cli.parent.mkdir(parents=True)
    cli.write_text(f"#!{sys.executable}\n" + (HERE / "fake_hyperframes.py").read_text())
    cli.chmod(0o755)
    return folder


def stage(
    data: Path, project: Path, composition: str = "index.html", at: str = "stage_2", **params: Any
) -> dict:
    """The stage `at` rendering the run's script; its output in that stage's own folder, the
    only one it may write."""
    script = data / RUN / "stage_1" / "artifacts" / "script.md"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# Why the chase works\n\nHook, then the reveal.\n")
    return {
        "calls": "hyperframes_render",
        "input": "script.md",
        "output": "short.mp4",
        "params": {
            "project": str(project),
            "composition": composition,
            "input_path": str(script),
            "output_path": str(data / RUN / at / "artifacts" / "short.mp4"),
            "frontmatter": {"world": W, "run": "run_1", "stage": "render"},
            **params,
        },
    }


def rendered(config: dict) -> dict:
    return json.loads(Path(config["params"]["output_path"]).read_text())


# The package


def test_the_manifest_passes_off_by_default_reaching_only_its_loopback() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["stage_types"]) == (
        "hyperframes_render",
        "stage",
        ["connector"],
    )
    assert (m["runtime"], m["enable_by_default"]) == ("uv", False)
    assert m["permissions"]["network"] == ["localhost"]
    assert m["params"]["required"] == ["project"]
    assert set(m["params"]["properties"]) == {
        "project",
        "composition",
        "quality",
        "fps",
        "variables",
    }
    assert m["params"]["properties"]["composition"]["default"] == "index.html"
    assert [i["media_type"] for i in m["inputs"]] == ["text/markdown"]
    assert [o["media_type"] for o in m["outputs"]] == ["video/mp4"]


def test_install_puts_its_python_the_hyperframes_cli_its_browser_and_ffmpeg_beside_it(
    installed: Path,
) -> None:
    folder = installed / "hyperframes_render" / "1.0.0"
    assert (folder / ".venv" / "bin" / "python").is_file()
    log = (folder / "fake-install.log").read_text().splitlines()
    assert log[0] == "npm ci --ignore-scripts --no-audit --no-fund"
    assert log[1:] == ["npm run ffmpeg", "npx --no hyperframes browser ensure --force"]


def test_a_world_has_it_only_once_its_operator_enables_it(installed: Path, tmp_path: Path) -> None:
    (tmp_path / "packages").symlink_to(installed)
    connectors = registry(tmp_path, FakeTransport())
    connectors.install_defaults()
    connectors.offer_defaults(W)
    assert connectors.find(W, "hyperframes_render") is None
    assert connectors.find(W, "remotion_render") is not None
    connectors.enable(W, "hyperframes_render", "1.0.0")
    assert connectors.find(W, "hyperframes_render") is not None


# Rendering over stdio


def test_progress_reads_the_cli_progress_lines() -> None:
    assert hr.progress("") == (0.0, "starting the render")
    log = 'Compiling\n@hf-progress {"code":"compile","pct":2}\n[INFO] trace\n'
    assert hr.progress(log) == (0.02, "compiling")
    log += '@hf-progress {"code":"capture","done":265,"total":300,"pct":65}\n'
    assert hr.progress(log) == (0.65, "capturing frames 265/300")


def test_renders_the_script_into_a_video_with_a_sidecar(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    config = stage(tmp_path, project, variables={"title": "Chase"}, quality="draft", fps=30)
    call = real.run_stage(W, "run_1", "stage_2", config)
    assert call.status == "done" and call.progress == 1.0 and call.polls >= 1
    video = Path(config["params"]["output_path"])
    assert call.response is not None and call.response["output"] == str(video)
    out = rendered(config)
    assert out["project"] == str(project) and out["flags"]["composition"] == "index.html"
    assert (out["flags"]["quality"], out["flags"]["fps"]) == ("draft", "30")
    assert out["script"].startswith("# Why the chase works") and out["title"] == "Chase"
    assert out["scriptPath"] == config["params"]["input_path"]

    sidecar = video.with_name("short.mp4.md")
    assert call.response["sidecar"] == str(sidecar)
    text = sidecar.read_text()
    head = text.split("---\n")[1]
    assert 'type: "video"' in head and 'world: "w"' in head and 'run: "run_1"' in head
    assert 'stage: "render"' in head and "created: " in head
    assert 'author: "hyperframes_render"' in head and 'composition: "index.html"' in head
    assert f"bytes: {video.stat().st_size}" in head
    assert "from the script `script.md` with composition `index.html`" in text


def test_telemetry_and_update_checks_are_off_and_its_cache_is_in_the_scratch_folder(
    tmp_path: Path, real: Connectors, project: Path, path_tools: Path
) -> None:
    config = stage(tmp_path, project)
    real.run_stage(W, "run_1", "stage_2", config)
    env = rendered(config)["env"]
    assert (env["HYPERFRAMES_NO_TELEMETRY"], env["DO_NOT_TRACK"]) == ("1", "1")
    assert env["HYPERFRAMES_NO_UPDATE_CHECK"] == env["HYPERFRAMES_NO_AUTO_INSTALL"] == "1"
    assert Path(env["HYPERFRAMES_CACHE_DIR"]).is_relative_to(tmp_path / "sandbox")
    assert env["HYPERFRAMES_BROWSER_PATH"] == str(path_tools / "chrome-headless-shell")
    assert env["HYPERFRAMES_FFMPEG_PATH"] == str(path_tools / "ffmpeg")
    assert env["HYPERFRAMES_FFPROBE_PATH"] == str(path_tools / "ffprobe")


@pytest.fixture
def own_tools(installed: Path) -> Iterator[dict[str, Path]]:
    """What the install step leaves beside the installed package, for one test: `browser
    ensure`'s Chrome Headless Shell in its cache, and `npm run ffmpeg`'s ffmpeg and ffprobe."""
    modules = installed / "hyperframes_render" / "1.0.0" / "node_modules"
    shell = "chrome/chrome-headless-shell/linux-148/chrome-headless-shell-linux64"
    tools = {
        "HYPERFRAMES_BROWSER_PATH": modules / ".hyperframes" / shell / "chrome-headless-shell",
        "HYPERFRAMES_FFMPEG_PATH": modules / ".ffmpeg" / "ffmpeg",
        "HYPERFRAMES_FFPROBE_PATH": modules / ".ffmpeg" / "ffprobe",
    }
    for tool in tools.values():
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    yield tools
    shutil.rmtree(modules)


def test_its_own_browser_and_ffmpeg_are_the_ones_used_else_those_on_path(
    tmp_path: Path, real: Connectors, project: Path, own_tools: dict[str, Path]
) -> None:
    config = stage(tmp_path, project)
    real.run_stage(W, "run_1", "stage_2", config)
    env = rendered(config)["env"]
    for var, tool in own_tools.items():
        assert Path(env[var]).resolve() == tool.resolve(), var


@pytest.mark.parametrize(
    ("tool", "named"), list(zip(TOOLS, ("Chrome", "ffmpeg", "ffprobe"), strict=True))
)
def test_a_render_without_chrome_or_ffmpeg_fails_saying_to_reinstall(
    tmp_path: Path, real: Connectors, project: Path, path_tools: Path, tool: str, named: str
) -> None:
    """A render cannot download them, having no network."""
    (path_tools / tool).unlink()
    with pytest.raises(ConnectorError, match=f"no {named}: reinstall hyperframes_render"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, project))
    assert not Path(stage(tmp_path, project)["params"]["output_path"]).exists()


def test_a_project_without_node_modules_is_refused_saying_how_to_install_them(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    """A render never installs: the sandbox has no network for it, and a project's
    package.json may be an agent's, whose scripts would run."""
    shutil.rmtree(project / "node_modules")
    with pytest.raises(ConnectorError, match="has no node_modules: run npm ci there"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, project))
    assert not (project / "node_modules").exists()


def test_the_sidecar_records_the_template_commit_of_a_git_checkout(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    config = stage(tmp_path, project)
    sidecar = Path(config["params"]["output_path"]).with_name("short.mp4.md")
    real.run_stage(W, "run_1", "stage_2", config)
    assert "commit: " not in sidecar.read_text()

    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "template"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    config = stage(tmp_path, project, at="stage_3")
    sidecar = Path(config["params"]["output_path"]).with_name("short.mp4.md")
    real.run_stage(W, "run_1", "stage_3", config)
    assert f'commit: "{sha.stdout.strip()}"' in sidecar.read_text()


def test_progress_reaches_the_call_while_it_renders(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    real.run_stage(W, "run_1", "stage_2", stage(tmp_path, project, "slow.html"))
    with closing(connect(tmp_path / "swarmify.db")) as conn:
        seen = [e.payload for e in read_after(conn, W, 0) if e.type == "connector_call.updated"]
    messages = [p["message"] for p in seen if p["status"] == "polling"]
    assert any(m and m.startswith("capturing frames ") for m in messages), messages
    values = [p["progress"] for p in seen if p["progress"] is not None]
    assert values == sorted(values) and values[-1] == 1.0


def test_a_failed_render_fails_the_stage_with_the_cli_output(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    with pytest.raises(ConnectorError) as raised:
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, project, "broken.html"))
    assert "hyperframes render exited with 1" in str(raised.value)
    assert "has no root data-composition-id" in str(raised.value)
    call = real.call_for("stage_2")
    assert call is not None and call.status == "failed"
    assert "has no root data-composition-id" in (call.error or "")


def test_a_render_that_writes_nothing_fails(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    with pytest.raises(ConnectorError, match="wrote no video"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, project, "empty.html"))


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"input_path": "/nope/script.md"}, "the script /nope/script.md does not exist"),
        ({"project": "/nope"}, "the HyperFrames project /nope does not exist"),
        ({"input_path": ""}, "the render stage needs an `input`"),
        ({"composition": "--output=/etc/x"}, "composition must be a plain value"),
        ({"quality": "--docker"}, "quality must be a plain value"),
    ],
)
def test_a_bad_request_is_refused_before_rendering(
    tmp_path: Path, real: Connectors, project: Path, over: dict, problem: str
) -> None:
    config = stage(tmp_path, project)
    config["params"].update(over)
    with pytest.raises(ConnectorError, match=problem):
        real.run_stage(W, "run_1", "stage_2", config)


def test_a_repeated_call_attaches_to_the_render_instead_of_starting_another(
    tmp_path: Path, real: Connectors, project: Path
) -> None:
    config = stage(tmp_path, project, "slow.html")
    request = {**config["params"], "idempotency_key": "stage_2"}
    connector = real.find(W, "hyperframes_render")
    assert connector is not None
    env = real._env(connector)
    transport = McpTransport()
    first = transport.call(connector, "hyperframes_render", request, env)
    again = transport.call(connector, "hyperframes_render", request, env)
    assert first["job_id"] == again["job_id"]
    assert (first["message"], again["message"]) == ("render started", "render already started")
    job = Path(first["job_id"])
    deadline = time.monotonic() + 10
    while json.loads(job.read_text())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    done = transport.call(connector, "hyperframes_render_status", {"job_id": str(job)}, env)
    assert done["status"] == "done"
    assert transport.call(connector, "hyperframes_render", request, env)["message"] == (
        "render already started"
    )


def test_pid_zero_is_never_a_live_render() -> None:
    assert not hr.alive(0) and hr.alive(os.getpid())


# A project an agent wrote renders in the connector sandbox


def test_a_built_project_renders_in_a_sandbox_it_cannot_leave(
    tmp_path: Path, given: Given, project: Path
) -> None:
    """The agent's code tries to read the data directory's keys, plant a file outside its
    stage and reach the internet: each is refused, and each is in the stage's run log."""
    (tmp_path / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cret\n")
    (tmp_path / ".env").chmod(0o600)
    connectors = given(McpTransport())
    blocked: list[tuple[Any, list[str]]] = []
    connectors.on_blocked = lambda c, at, lines: blocked.append((at, lines))
    config = stage(tmp_path, project, "snoop.html")
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    snooped = rendered(config)["snooped"]
    assert set(snooped) == {"read_keys", "write_outside", "network"}
    assert snooped["read_keys"].startswith("refused") and snooped["network"].startswith("refused")
    # Linux hides the data directory behind an empty folder of the sandbox's own, which takes
    # the write and is gone with it; macOS refuses it
    if sys.platform != "linux":
        assert snooped["write_outside"].startswith("refused"), snooped
    assert not (tmp_path / "planted.txt").exists()
    lines = [line for _, said in blocked for line in said]
    assert {at for at, _ in blocked} == {("run_1", "stage_2")}
    assert f"read {(tmp_path / '.env').resolve()}" in lines, lines
    assert f"write {(tmp_path / 'planted.txt').resolve()}" in lines, lines
    assert any(line.startswith("connect example.com") for line in lines), lines


# The real thing, on an operator's machine


@pytest.mark.skipif(
    not (os.environ.get("SWARMIFY_REAL_BUILD") and os.environ.get("SWARMIFY_HYPERFRAMES_PROJECT")),
    reason="set SWARMIFY_REAL_BUILD=1 and SWARMIFY_HYPERFRAMES_PROJECT for a real render",
)
def test_a_real_render(tmp_path: Path, given: Given) -> None:
    """The real install (npm, ffmpeg, Chrome) and a real render of a project with its
    `node_modules`, copied where a build would have written it, offline in the sandbox."""
    connectors = given(McpTransport())
    blocked: list[str] = []
    connectors.on_blocked = lambda c, at, lines: blocked.extend(lines)
    folder = tmp_path / RUN / "stage_1" / "build"
    shutil.copytree(os.environ["SWARMIFY_HYPERFRAMES_PROJECT"], folder, symlinks=True)
    config = stage(tmp_path, folder)
    call = connectors.run_stage(W, "run_1", "stage_2", config)
    video = Path(config["params"]["output_path"])
    print("blocked:", blocked)
    assert call.status == "done" and video.stat().st_size > 1000
    assert video.read_bytes()[4:8] == b"ftyp"
    assert video.with_name("short.mp4.md").exists()
