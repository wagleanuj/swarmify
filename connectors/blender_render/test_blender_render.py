"""The Blender render connector: the package installed through the real install path (with the
suite's fake `npm`, so nothing is downloaded), enabled for a world, started over stdio MCP in the
connector sandbox and rendering with a fake Blender and ffmpeg on PATH (`fake_blender.py`,
`fake_ffmpeg.py`), its project one of the world's Templates or a build folder of the run. Some
renders run in this process, as remotion_render's tests do, to watch the job closely. With a real
`blender` and `ffmpeg` on PATH one 3-frame shot renders for real."""

import base64
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import blender_render as br
import fetch
import pytest
from mcp import Client, StdioServerParameters

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
from swarmify_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
W = "w"
Given = Callable[..., Connectors]
RUN = Path("worlds") / W / "runs" / "run_1"
TEMPLATES = Path("worlds") / W / "assets" / "blender_templates"


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One install for the module, as Swarmify does at startup: installed, not a default."""
    data = tmp_path_factory.mktemp("data")
    defaults = registry(data, FakeTransport()).install_defaults()
    assert "blender_render" not in defaults
    return data / "packages"


@pytest.fixture
def given(installed: Path, tmp_path: Path) -> Given:
    def make(transport: Any, data: Path = tmp_path) -> Connectors:
        data.mkdir(exist_ok=True)
        (data / "packages").symlink_to(installed)
        connectors = registry(data, transport)
        connectors.install_defaults()  # installed already: only remembered
        connectors.offer_defaults(W)
        connectors.enable(W, "blender_render", "1.0.3")
        return connectors

    return make


@pytest.fixture
def path_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Blender, ffmpeg and ffprobe first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("blender", "fake_blender.py"), ("ffmpeg", "fake_ffmpeg.py")):
        (bin_dir / name).write_text(f"#!{sys.executable}\n" + (HERE / source).read_text())
        (bin_dir / name).chmod(0o755)
    os.link(bin_dir / "ffmpeg", bin_dir / "ffprobe")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("SWARMIFY_GPU_LOCK", raising=False)
    return bin_dir


SHOT = "import bpy  # {note}\n# frames: {frames}\n"
FINISH = """import json, os, shutil, sys
out = {k: v for k, v in os.environ.items() if k.startswith("BLENDER_")}
out["ffmpeg"], out["ffprobe"] = shutil.which("ffmpeg"), shutil.which("ffprobe")
out["python3"], out["cwd"] = shutil.which("python3"), os.getcwd()
out["shots"] = sorted(os.listdir(os.environ["BLENDER_SHOTS"]))
if "--nothing" not in sys.argv:
    open(os.environ["BLENDER_OUTPUT"], "w").write(json.dumps(out))
"""


def make_project(folder: Path, finish: list[str] | None = None, **notes: str) -> Path:
    """A Blender project with shots s01 (3 frames) and s02 (2 frames), a shared kit module and
    a finish script; `notes` adds a directive comment to a shot's script (`fail`, `slow`)."""
    (folder / "shots").mkdir(parents=True)
    (folder / "kit").mkdir()
    (folder / "tools").mkdir()
    (folder / "kit" / "common.py").write_text("LIGHT = 1\n")
    (folder / "tools" / "finish.py").write_text(FINISH)
    for sid, frames in (("s01", 3), ("s02", 2)):
        note = notes.get(sid, "")
        (folder / "shots" / f"{sid}.py").write_text(SHOT.format(note=note, frames=frames))
    spec: dict[str, Any] = {
        "fps": 30,
        "width": 18,
        "height": 32,
        "shots": [{"id": "s01", "script": "shots/s01.py"}, {"id": "s02", "script": "shots/s02.py"}],
        "quality": {"draft": {}, "final": {}},
    }
    if finish:
        spec["finish"] = finish
    (folder / "render.json").write_text(json.dumps(spec))
    return folder


def stage(data: Path, project: Path, at: str = "stage_2", **params: Any) -> dict:
    return {
        "calls": "blender_render",
        "output": "video.mp4",
        "params": {
            "project": str(project),
            "output_path": str(data / RUN / at / "artifacts" / "video.mp4"),
            "frontmatter": {"world": W, "run": "run_1", "stage": at},
            **params,
        },
    }


def records(folder: Path, event: str = "start") -> list[dict]:
    """The fake Blender's runs that wrote into a subfolder of `folder`."""
    file = folder / "calls.jsonl"
    lines = file.read_text().splitlines() if file.exists() else []
    return [r for r in map(json.loads, lines) if r["event"] == event]


def calls(output: Path, event: str = "start") -> list[dict]:
    """The fake Blender's runs for the stage of `output`."""
    return records(output.parents[1] / "frames", event)


def here(params: dict, key: str = "k") -> tuple[Path, list[dict]]:
    """A render started in this process; its job, polled until it ends, and each poll's answer."""
    job = Path(br.blender_render(idempotency_key=key, **params)["job_id"])
    seen, deadline = [], time.monotonic() + 30
    while (status := br.blender_render_status(str(job)))["status"] == "running":
        assert time.monotonic() < deadline
        seen.append(status)
        time.sleep(0.02)
    return job, [*seen, status]


# The package


def test_the_manifest_passes_off_by_default_on_the_gpu_with_no_network() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["runtime"], m["enable_by_default"]) == (
        "blender_render",
        "stage",
        "uv",
        False,
    )
    assert m["capabilities"] == ["templates"]
    assert m["templates"] == {"folder": "blender_templates", "require": ["render.json"]}
    assert m["permissions"]["gpu"] is True and "network" not in m["permissions"]
    assert m["permissions"]["folders"] == ["worlds/{world}/assets"]
    assert m["params"]["required"] == ["project"]
    assert [o["media_type"] for o in m["outputs"]] == ["video/mp4"]
    (version,) = m["config"]
    assert (version["env"], version["default"]) == (br.VERSION, br.DEFAULT_VERSION)
    assert br.DEFAULT_VERSION in fetch.BLENDER


def test_install_syncs_its_python_then_fetches_blender_and_ffmpeg(installed: Path) -> None:
    folder = installed / "blender_render" / "1.0.3"
    assert (folder / ".venv" / "bin" / "python").exists()
    assert (folder / "fake-install.log").read_text() == "npm run fetch --silent\n"


def test_a_blender_version_without_a_pinned_checksum_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nothing(*_: Any, **__: Any) -> None:
        raise AssertionError("downloaded")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", nothing)
    with pytest.raises(SystemExit, match="Blender 9.9.9 has no pinned checksum here"):
        fetch.main(["9.9.9"])
    monkeypatch.setenv(br.VERSION, "9.9.9")
    with pytest.raises(br.ToolError, match="Blender 9.9.9 has no pinned checksum"):
        br.blender()


def test_a_download_that_does_not_match_its_pin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tarball"
    source.write_bytes(b"not blender")
    to = tmp_path / "got"
    with pytest.raises(SystemExit, match="not the pinned"):
        fetch.download(source.as_uri(), "0" * 64, to)
    assert not to.exists()


def test_on_linux_x64_ffmpeg_and_ffprobe_come_from_the_pinned_tarball(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "ffmpeg-n8-linux64-gpl"
    (source / "bin").mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe", "ffplay"):
        (source / "bin" / name).write_text(name)
        (source / "bin" / name).chmod(0o755)
    tarball = tmp_path / "ffmpeg.tar.xz"
    subprocess.run(["tar", "-cJf", tarball, "-C", source.parent, source.name], check=True)
    sha = fetch.hashlib.sha256(tarball.read_bytes()).hexdigest()
    monkeypatch.setattr(fetch, "VENDOR", tmp_path / "vendor")
    monkeypatch.setattr(fetch.sys, "platform", "linux")
    monkeypatch.setattr(fetch.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(fetch, "FFMPEG_LINUX_X64", (tarball.as_uri(), "0" * 64))
    with pytest.raises(SystemExit, match="not the pinned"):
        fetch.ffmpeg()
    monkeypatch.setattr(fetch, "FFMPEG_LINUX_X64", (tarball.as_uri(), sha))
    fetch.ffmpeg()
    got = sorted(p.name for p in (tmp_path / "vendor" / "bin").iterdir())
    assert got == ["ffmpeg", "ffprobe"]
    assert os.access(tmp_path / "vendor" / "bin" / "ffmpeg", os.X_OK)


@pytest.mark.skipif(
    not (fetch.VENDOR / "bin" / "ffmpeg").is_file(), reason="needs the fetched ffmpeg"
)
def test_the_fetched_ffmpeg_has_the_filters_a_finish_step_is_promised() -> None:
    out = subprocess.run(
        [fetch.VENDOR / "bin" / "ffmpeg", "-hide_banner", "-filters"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    filters = {line.split()[1] for line in out.splitlines() if len(line.split()) > 2}
    assert {"drawtext", "ass", "subtitles"} <= filters
    assert "--enable-libx264" in subprocess.run(
        [fetch.VENDOR / "bin" / "ffmpeg", "-hide_banner", "-buildconf"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def test_progress_reads_shots_the_gpu_wait_and_the_finish() -> None:
    assert br.progress("") == (0.0, "starting the render")
    log = "Shot 1/3: s01\nBlender\nShot 2/3: s02\n"
    assert br.progress(log) == (0.3, "shot 2 of 3 (s02)")
    assert br.progress(log + "Waiting for the GPU\n") == (0.3, "waiting for the GPU")
    assert br.progress(log + "Shot 3/3: s03\nFinishing\n") == (0.9, "finishing")


# Rendering


def test_shots_render_in_order_on_the_gpu_and_are_joined_without_a_finish(
    tmp_path: Path, given: Given, path_tools: Path
) -> None:
    """Over stdio MCP in the sandbox: each shot a Blender process with the documented
    environment, reading the world's Assets; their frames encoded to a video each, then joined
    in render.json's order."""
    connectors = given(McpTransport())
    assets = tmp_path / "worlds" / W / "assets"
    (assets / "rigs").mkdir(parents=True)
    project = make_project(tmp_path / TEMPLATES / "short")
    config = stage(tmp_path, project)
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    output = Path(config["params"]["output_path"])
    stage_dir = output.parents[1]
    video = json.loads(output.read_text())
    assert video["inputs"] == [str(stage_dir / "shots/s01.mp4"), str(stage_dir / "shots/s02.mp4")]
    first, second = calls(output)
    assert [first["env"]["BLENDER_SHOT"], second["env"]["BLENDER_SHOT"]] == ["s01", "s02"]
    assert first["args"][6:] == [str(project / "shots/s01.py"), "--", "draft"]
    assert first["env"] == {
        "BLENDER_PROJECT": str(project),
        "BLENDER_SHOT": "s01",
        "BLENDER_SHOT_OUT": str(stage_dir / "frames/s01"),
        "BLENDER_SHOT_QUALITY": "draft",
        "BLENDER_SHOT_FRAMES": "",
        "BLENDER_ASSETS": str(assets),
        "__GL_SHADER_DISK_CACHE_PATH": str(stage_dir / "shader-cache"),
        "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP": "1",
    }
    assert "rigs" in first["assets"]  # the world's Assets are readable
    shot = json.loads((stage_dir / "shots/s01.mp4").read_text())
    assert shot["inputs"] == ["frame_0001.png", "frame_0002.png", "frame_0003.png"]
    assert ["-framerate", "30"] == shot["args"][4:6] and "scale=18:32" in shot["args"]
    log = br._hidden(output, "log").read_text()
    assert "Saved: '" in log and "Fra:" not in log  # Blender's lines, but not its frame noise
    assert 'author: "blender_render"' in output.with_name("video.mp4.md").read_text()


def test_an_unchanged_shot_is_skipped_and_a_change_rerenders_what_it_touches(
    tmp_path: Path, path_tools: Path
) -> None:
    project = make_project(tmp_path / RUN / "stage_2" / "build")
    params = stage(tmp_path, project)["params"]
    output = Path(params["output_path"])

    def rendered(key: str) -> list[str]:
        before = len(calls(output))
        job, _ = here(params, key)
        assert json.loads(job.read_text())["status"] == "done"
        return [c["env"]["BLENDER_SHOT"] for c in calls(output)[before:]]

    assert rendered("1") == ["s01", "s02"]
    assert rendered("2") == []
    (project / "shots/s02.py").write_text(SHOT.format(note="lit", frames=2))
    assert rendered("3") == ["s02"]
    (project / "kit" / "__pycache__").mkdir()
    (project / "kit" / "__pycache__" / "common.pyc").write_text("x")  # not a shared file
    assert rendered("4") == []
    (project / "kit" / "common.py").write_text("LIGHT = 2\n")
    assert rendered("5") == ["s01", "s02"]
    params["quality"] = "final"
    assert rendered("6") == ["s01", "s02"]


def test_a_finish_step_makes_the_video_with_the_documented_environment(
    tmp_path: Path, given: Given, path_tools: Path
) -> None:
    connectors = given(McpTransport())
    project = make_project(tmp_path / TEMPLATES / "short", finish=["python3", "tools/finish.py"])
    config = stage(tmp_path, project)
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    output = Path(config["params"]["output_path"])
    made = json.loads(output.read_text())
    package = tmp_path / "packages" / "blender_render" / "1.0.3"
    assert {k: v for k, v in made.items() if k.startswith("BLENDER_")} == {
        "BLENDER_PROJECT": str(project),
        "BLENDER_ASSETS": str(tmp_path / "worlds" / W / "assets"),
        "BLENDER_SHOTS": str(output.parents[1] / "shots"),
        "BLENDER_RUN": str(tmp_path / RUN),
        "BLENDER_OUTPUT": str(output),
    }
    assert made["ffmpeg"] == str(path_tools / "ffmpeg") and made["ffprobe"]
    assert Path(made["python3"]).parent.resolve() == (package / ".venv" / "bin").resolve()
    assert made["cwd"] == str(project) and made["shots"] == ["s01.mp4", "s02.mp4"]


@pytest.mark.parametrize(
    ("notes", "finish", "said"),
    [
        ({"s02": "fail"}, None, "shot s02: Blender exited with 1"),
        ({"s02": "empty"}, None, "shot s02 wrote no frames"),
        ({}, ["python3", "tools/finish.py", "--nothing"], "the finish step wrote no video"),
        ({}, ["python3", "tools/missing.py"], "the finish step exited with"),
    ],
)
def test_a_failure_fails_the_stage_naming_its_cause_with_the_log(
    tmp_path: Path, given: Given, path_tools: Path, notes: dict, finish: list | None, said: str
) -> None:
    connectors = given(McpTransport())
    project = make_project(tmp_path / TEMPLATES / "short", finish=finish, **notes)
    with pytest.raises(ConnectorError) as raised:
        connectors.run_stage(W, "run_1", "stage_2", stage(tmp_path, project))
    assert said in str(raised.value)
    if "fail" in notes.values():
        assert "RuntimeError: boom in the shot script" in str(raised.value)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda p: (p / "render.json").unlink(), "has no render.json"),
        (lambda p: (p / "render.json").write_text('{"fps": 30}'), "needs `width`"),
        (
            lambda p: (p / "render.json").write_text(
                json.dumps(
                    {"fps": 1, "width": 1, "height": 1, "shots": [{"id": "a", "script": "../x.py"}]}
                )
            ),
            "shot a: its script '../x.py' is not in the project",
        ),
    ],
)
def test_a_bad_project_is_refused_before_rendering(
    tmp_path: Path, path_tools: Path, change: Callable[[Path], Any], problem: str
) -> None:
    project = make_project(tmp_path / "p")
    change(project)
    with pytest.raises(br.ToolError, match=problem):
        here(stage(tmp_path, project)["params"])


def test_a_quality_the_project_does_not_offer_is_refused(tmp_path: Path, path_tools: Path) -> None:
    params = stage(tmp_path, make_project(tmp_path / "p"), quality="ultra")["params"]
    with pytest.raises(
        br.ToolError, match="quality ultra is not one of render.json's: draft, final"
    ):
        here(params)


def test_progress_reports_shot_n_of_m(tmp_path: Path, path_tools: Path) -> None:
    project = make_project(tmp_path / "p", s01="slow", s02="slow")
    _, seen = here(stage(tmp_path, project)["params"])
    messages = [s.get("message") for s in seen]
    assert "shot 1 of 2 (s01)" in messages and "shot 2 of 2 (s02)" in messages
    values = [s["progress"] for s in seen]
    assert values == sorted(values) and values[-1] == 1


def test_without_a_gpu_it_renders_on_the_cpu_saying_so(
    tmp_path: Path, path_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(br, "DEV", tmp_path / "dev")
    params = stage(tmp_path, make_project(tmp_path / "p"))["params"]
    job, _ = here(params)
    assert br.NO_GPU in Path(json.loads(job.read_text())["log"]).read_text()
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "nvidia0").write_text("")
    job, _ = here(params, "again")
    assert br.NO_GPU not in Path(json.loads(job.read_text())["log"]).read_text()


def test_cancelling_the_render_stops_blender(tmp_path: Path, path_tools: Path) -> None:
    project = make_project(tmp_path / "p", s01="slow\n# frames: 100")
    params = stage(tmp_path, project)["params"]
    output = Path(params["output_path"])
    job = Path(br.blender_render(idempotency_key="k", **params)["job_id"])
    deadline = time.monotonic() + 30
    while not calls(output):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    (started,) = calls(output)
    cancelled = br.blender_render_status(str(job), cancel=True)  # its SIGTERM handler ends it
    assert cancelled["error"].startswith("the render was cancelled")
    while br.alive(started["pid"]):  # killed, then reaped by the job
        assert time.monotonic() < deadline, "Blender outlived the cancel"
        time.sleep(0.02)
    assert calls(output, "end") == []


def test_a_job_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(br, "GRACE", 0.3)
    stubborn = subprocess.Popen(
        [sys.executable, "-c", IGNORES_SIGTERM], start_new_session=True, stdout=subprocess.PIPE
    )
    assert stubborn.stdout is not None and stubborn.stdout.readline() == b"ready\n"
    job = tmp_path / ".video.mp4.job.json"
    job.write_text(json.dumps({"status": "running", "pid": stubborn.pid, "log": "/nonexistent"}))
    started = time.monotonic()
    answer = br.blender_render_status(str(job), cancel=True)
    assert answer["status"] == "failed" and answer["error"].startswith("the render was cancelled")
    assert stubborn.wait(timeout=5) == -signal.SIGKILL
    assert 0.3 <= time.monotonic() - started < 5


IGNORES_SIGTERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',"
    " flush=True); time.sleep(60)"
)


def test_each_blender_process_holds_the_gpu_lock(
    tmp_path: Path, path_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two renders at once share the card one Blender process at a time, and one says it waits."""
    lock = tmp_path / "gpu.lock"
    monkeypatch.setenv("SWARMIFY_GPU_LOCK", str(lock))
    project = make_project(tmp_path / "p", s01="slow", s02="slow")
    jobs = [
        Path(
            br.blender_render(idempotency_key=at, **stage(tmp_path, project, at)["params"])[
                "job_id"
            ]
        )
        for at in ("stage_2", "stage_3")
    ]
    deadline = time.monotonic() + 60
    while any(br.blender_render_status(str(j))["status"] == "running" for j in jobs):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert all(br.blender_render_status(str(j))["status"] == "done" for j in jobs)
    runs = sorted(
        (start["start"], end["end"])
        for at in ("stage_2", "stage_3")
        for start, end in zip(
            calls(tmp_path / RUN / at / "artifacts/video.mp4"),
            calls(tmp_path / RUN / at / "artifacts/video.mp4", "end"),
            strict=True,
        )
    )
    assert len(runs) == 4
    assert all(a[1] <= b[0] for a, b in zip(runs, runs[1:], strict=False)), runs
    logs = [Path(json.loads(j.read_text())["log"]).read_text() for j in jobs]
    assert any(br.WAITING in log for log in logs)


def test_a_render_waits_while_another_gpu_job_holds_the_card(
    tmp_path: Path, path_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "gpu.lock"
    monkeypatch.setenv("SWARMIFY_GPU_LOCK", str(lock))
    params = stage(tmp_path, make_project(tmp_path / "p"))["params"]
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        job = Path(br.blender_render(idempotency_key="k", **params)["job_id"])
        deadline = time.monotonic() + 30
        while (status := br.blender_render_status(str(job)))["message"] != "waiting for the GPU":
            assert status["status"] == "running" and time.monotonic() < deadline, status
            time.sleep(0.02)
        assert calls(Path(params["output_path"])) == []
    while br.blender_render_status(str(job))["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert br.blender_render_status(str(job))["status"] == "done"


REAL_SHOT = """import os, bpy
scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y = 64, 112
scene.frame_start, scene.frame_end = 1, 3
bpy.ops.mesh.primitive_cube_add()
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = os.path.join(os.environ["BLENDER_SHOT_OUT"], "frame_####")
bpy.ops.render.render(animation=True)
"""


@pytest.mark.skipif(
    not (shutil.which("blender") and shutil.which("ffmpeg")),
    reason="a real render needs blender and ffmpeg on PATH",
)
def test_a_real_blender_renders_a_3_frame_shot(tmp_path: Path) -> None:
    project = tmp_path / "real"
    (project / "shots").mkdir(parents=True)
    (project / "shots/s01.py").write_text(REAL_SHOT)
    spec = {
        "fps": 30,
        "width": 64,
        "height": 112,
        "shots": [{"id": "s01", "script": "shots/s01.py"}],
    }
    (project / "render.json").write_text(json.dumps(spec))
    params = stage(tmp_path, project)["params"]
    job, _ = here(params)
    state = json.loads(job.read_text())
    assert state["status"] == "done", br._tail(Path(state["log"]))
    frames = Path(params["output_path"]).parents[1] / "frames/s01"
    assert sorted(p.name for p in frames.glob("*.png")) == [f"frame_000{n}.png" for n in (1, 2, 3)]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", params["output_path"]],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == "3"


# Stills, as a Build Session calls them (#403): the server confined to the build's stage


def still(connectors: Connectors, **arguments: Any) -> Any:
    """`stills` called over stdio MCP on the server a Build Session of stage_2 is given."""
    server = connectors.agent_servers(W, ["blender_render"], None, ("run_1", "stage_2"))
    config = server["blender_render"]
    params = StdioServerParameters(
        command=config["command"], args=config["args"], env=config["env"]
    )

    async def call() -> Any:
        async with Client(params, cache=None) as client:
            return await client.call_tool("stills", arguments)

    return anyio.run(call)


@pytest.fixture
def build(tmp_path: Path, path_tools: Path) -> Path:
    return make_project(tmp_path / RUN / "stage_2" / "build", s02="empty")


def test_stills_render_only_the_frames_asked_into_one_labelled_sheet(
    tmp_path: Path, given: Given, build: Path
) -> None:
    from PIL import Image as Picture  # pyright: ignore[reportMissingImports]

    result = still(given(McpTransport()), project=str(build), shot="s01", frames=[3, 1])
    assert not result.is_error, result
    image, path = result.content
    assert (image.type, image.mime_type, path.type) == ("image", "image/png", "text")
    sheet = Path(path.text)
    assert sheet == build.parent / "stills" / "s01.png"
    assert base64.b64decode(image.data) == sheet.read_bytes()
    with Picture.open(sheet) as picture:
        assert picture.size == (2 * 270, 480 + 28)  # two 18x32 frames scaled to 480 high
    (run,) = records(build.parent / "stills")
    assert run["env"]["BLENDER_SHOT_FRAMES"] == "1,3"
    assert run["env"]["BLENDER_SHOT_OUT"] == str(build.parent / "stills" / "s01")
    assert run["env"]["__GL_SHADER_DISK_CACHE_PATH"] == str(build.parent / "shader-cache")
    assert run["env"]["BLENDER_ASSETS"] == str(tmp_path / "worlds" / W / "assets")
    assert sorted(p.name for p in (build.parent / "stills" / "s01").iterdir()) == [
        "frame_0001.png",
        "frame_0003.png",
    ]


def test_the_sheet_labels_each_tile_with_its_shot_and_frame(tmp_path: Path) -> None:
    from PIL import Image as Picture  # pyright: ignore[reportMissingImports]

    frame = tmp_path / "f.png"
    Picture.new("RGB", (18, 32), (40, 80, 160)).save(frame)
    plain, labelled = tmp_path / "plain.png", tmp_path / "labelled.png"
    br.contact_sheet([(frame, "")], plain, 18, 32)
    br.contact_sheet([(frame, "s01 frame 12")], labelled, 18, 32)
    with Picture.open(plain) as a, Picture.open(labelled) as b:
        strip = (0, 0, a.width, 28)
        assert a.crop(strip).getextrema() == ((0, 0), (0, 0), (0, 0))  # black
        assert b.crop(strip).getextrema() != a.crop(strip).getextrema()  # the label drawn
        assert (
            a.crop((0, 28, a.width, a.height)).tobytes()
            == b.crop((0, 28, b.width, b.height)).tobytes()
        )


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"shot": "s09", "frames": [1]}, "no shot 's09' in render.json; its shots: s01, s02"),
        ({"shot": "s02", "frames": [1, 2]}, "shot s02 wrote no frames"),
        ({"shot": "s01", "frames": [2, 7]}, "shot s01 wrote no frame [7]"),
        ({"shot": "s01", "frames": []}, "frames must be 1 to 24 frame numbers from 1"),
        ({"shot": "s01", "frames": [1], "quality": "ultra"}, "quality ultra is not one of"),
    ],
)
def test_stills_that_cannot_make_a_sheet_say_why(
    given: Given, build: Path, arguments: dict, said: str
) -> None:
    result = still(given(McpTransport()), project=str(build), **arguments)
    assert result.is_error
    assert said in result.content[0].text
    assert not (build.parent / "stills" / f"{arguments['shot']}.png").exists()


def test_stills_wait_for_the_gpu_like_the_render(
    tmp_path: Path, build: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "gpu.lock"
    monkeypatch.setenv("SWARMIFY_GPU_LOCK", str(lock))
    log = build.parent / "stills" / "s01.log"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with ThreadPoolExecutor(1) as pool:
            done = pool.submit(br.stills, str(build), "s01", [1])
            deadline = time.monotonic() + 30
            while not (log.exists() and br.WAITING in log.read_text()):
                assert time.monotonic() < deadline and not done.done()
                time.sleep(0.02)
            assert not (build.parent / "stills" / "calls.jsonl").exists()  # no Blender yet
            fcntl.flock(held, fcntl.LOCK_UN)
            assert done.result(timeout=30)[1] == str(build.parent / "stills" / "s01.png")
