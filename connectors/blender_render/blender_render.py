"""The Blender render connector: a stdio MCP server that renders a Blender project's shots with the
Blender its install step put beside it (ADR 0015).

A project is a folder with a `render.json` (README.md, The project contract): its shots in order,
each a Blender Python script, optional quality presets and an optional finish command. Each shot
runs as its own Blender process, `blender -b --factory-startup -noaudio --python-exit-code 1
--python <script> -- <quality>`, writing PNG frames into a folder its environment names; the
package encodes each shot to an mp4, then the finish command makes the video from them, or with
none the shots are joined in order.

`blender_render` starts one render per idempotency key and answers with a long job;
`blender_render_status` reports its progress (shot n of m, waiting for the GPU, finishing). The
render runs in a detached process (`--job <file>`) so it outlives the MCP session; its state and
log live beside the output as hidden files. A shot whose stamp (its script, the project's shared
files, the quality) is unchanged and whose frames are there is not rendered again. SIGTERM to the
job (a cancel) kills the Blender it runs and fails the job.

Each Blender process holds the shared GPU lock (`SWARMIFY_GPU_LOCK`) while it runs, and NVIDIA's
shader disk cache is a folder of the stage, so only the first process of a stage compiles shaders.

It is a Connector Package (ADR 0019): `swarmify-connector.toml` beside it is everything Swarmify
knows of it. It runs in the connector sandbox (ADR 0021), which also holds the shot scripts it
executes: it reads this folder, the world's Assets and the run's files, writes only its stage's
folder and its scratch folder, and reaches no network.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import fetch
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

POLL = "blender_render_status"
TAIL = 40
VERSION = "SWARMIFY_BLENDER_VERSION"
DEFAULT_VERSION = "5.2.1"
GPU_LOCK = "SWARMIFY_GPU_LOCK"
"""The lock file every process granted the GPU shares, which each Blender process holds."""
WAITING = "Waiting for the GPU"
FINISHING = "Finishing"
DEV = Path("/dev")
GPU = ("nvidia0", "dri/renderD*")
"""A GPU the sandbox handed this process (its Manifest's `permissions.gpu`)."""
NO_GPU = "No GPU here: Blender renders on the CPU, slowly"
NOT_SHARED = {"__pycache__", ".git", "node_modules", "renders", "output"}
"""Folders of a project that are not its shared files: what Python and tools leave behind, and
renders a project keeps of its own."""
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SHOT = re.compile(r"^Shot (\d+)/(\d+): (\S+)", re.M)

mcp = MCPServer("blender_render")


class Cancelled(Exception):
    """SIGTERM reached the render job."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_folder(project: str) -> Path:
    """The project folder, which Swarmify gives as a full path: a world's Template it resolved
    from the stage's `project`, or a build folder of the run."""
    path = Path(project)
    if not path.is_absolute():
        raise ToolError(f"project must be a folder's full path, got {project!r}")
    if not path.is_dir():
        raise ToolError(f"the Blender project {path} does not exist")
    return path


def load(folder: Path) -> dict[str, Any]:
    """The project's `render.json`, checked: shots each with a plain `id` and a `script` inside
    the project, positive `fps`, `width` and `height`, `quality` presets by name and `finish` an
    argv."""
    try:
        spec = json.loads((folder / "render.json").read_text())
    except FileNotFoundError:
        raise ToolError(f"{folder} has no render.json") from None
    except ValueError as exc:
        raise ToolError(f"render.json is not JSON: {exc}") from None
    if not isinstance(spec, dict):
        raise ToolError("render.json must be an object")
    for key in ("fps", "width", "height"):
        if not isinstance(spec.get(key), int) or spec[key] < 1:
            raise ToolError(f"render.json needs `{key}`, a positive whole number")
    shots = spec.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ToolError("render.json lists no `shots`")
    seen: set[str] = set()
    for shot in shots:
        sid = shot.get("id") if isinstance(shot, dict) else None
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in seen:
            raise ToolError(f"each shot needs a plain, unique `id`, got {sid!r}")
        seen.add(sid)
        script = (folder / str(shot.get("script", ""))).resolve()
        if not script.is_relative_to(folder.resolve()) or not script.is_file():
            raise ToolError(f"shot {sid}: its script {shot.get('script')!r} is not in the project")
    if not isinstance(spec.get("quality", {}), dict):
        raise ToolError("render.json's `quality` must be an object of presets by name")
    finish = spec.get("finish")
    if finish is not None and not (
        isinstance(finish, list) and finish and all(isinstance(a, str) for a in finish)
    ):
        raise ToolError("render.json's `finish` must be a command, as a list of strings")
    return spec


def quality_of(spec: dict[str, Any], quality: str) -> str:
    if not _ID.fullmatch(quality):
        raise ToolError(f"quality must be a plain name, got {quality!r}")
    presets = spec.get("quality")
    if presets and quality not in presets:
        raise ToolError(f"quality {quality} is not one of render.json's: {', '.join(presets)}")
    return quality


def blender() -> str:
    """The Blender a render runs: the Setup's version (a pinned one) the install step put in
    vendor/, else a `blender` on PATH."""
    version = os.environ.get(VERSION) or DEFAULT_VERSION
    if version not in fetch.BLENDER:
        pinned = ", ".join(fetch.BLENDER)
        raise ToolError(f"Blender {version} has no pinned checksum in blender_render: {pinned}")
    own = fetch.VENDOR / "blender" / version / "blender"
    found = str(own) if own.is_file() else shutil.which("blender")
    if not found:
        raise ToolError(f"no Blender {version}: reinstall blender_render")
    return found


def tool(name: str) -> str:
    """ffmpeg or ffprobe: the install step's, else one on PATH."""
    own = fetch.VENDOR / "bin" / name
    found = str(own) if own.is_file() else shutil.which(name)
    if not found:
        raise ToolError(f"no {name}: reinstall blender_render")
    return found


def assets_of(folder: Path) -> str:
    """The world's Assets folder for a project inside a world (a Template or a run's build)."""
    world = next((p for p in folder.parents if p.parent.name == "worlds"), None)
    return str(world / "assets") if world else ""


def _gpu_here() -> bool:
    return any(p.exists() for pattern in GPU for p in DEV.glob(pattern))


@contextmanager
def _gpu(log: IO[str]) -> Iterator[None]:
    """The card to this process alone while it runs: an exclusive flock on the shared GPU lock,
    saying in the log when another GPU job holds it. Without the variable, no lock."""
    path = os.environ.get(GPU_LOCK)
    if not path:
        yield
        return
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.write(f"{WAITING}\n")
            log.flush()
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield  # closing the file lets it go


def _run(argv: list[str], cwd: Path, env: dict[str, str], log: IO[str]) -> int:
    """One process, its output in `log` but Blender's per-frame status lines (`Fra:`); its exit
    code. Killed if this process is stopped while it runs."""
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as exc:
        log.write(f"could not start {argv[0]}: {exc}\n")
        return -1
    try:
        assert child.stdout is not None
        for line in child.stdout:
            if not line.startswith("Fra:"):
                log.write(line)
                log.flush()
        return child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def shot(
    folder: Path,
    entry: dict[str, Any],
    quality: str,
    out: Path,
    frames: str,
    assets: str,
    cache: Path,
    log: IO[str],
) -> int:
    """One shot rendered by its own Blender process into `out` (all frames, or the comma-separated
    `frames`) while it holds the GPU; Blender's exit code."""
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "BLENDER_PROJECT": str(folder),
        "BLENDER_SHOT": entry["id"],
        "BLENDER_SHOT_OUT": str(out),
        "BLENDER_SHOT_QUALITY": quality,
        "BLENDER_SHOT_FRAMES": frames,
        "BLENDER_ASSETS": assets,
        # NVIDIA's compiled shaders, kept for the stage: a cold process spends about 11 s on them
        "__GL_SHADER_DISK_CACHE_PATH": str(cache),
        "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP": "1",
    }
    script = str(folder / entry["script"])
    argv = [blender(), "-b", "--factory-startup", "-noaudio", "--python-exit-code", "1"]
    argv += ["--python", script, "--", quality]
    with _gpu(log):
        return _run(argv, folder, env, log)


def _tail(log: Path) -> str:
    text = log.read_text(errors="replace") if log.exists() else ""
    return "\n".join(text.strip().splitlines()[-TAIL:])


# The render


def _hidden(output: Path, suffix: str) -> Path:
    return output.with_name(f".{output.name}.{suffix}")


def _read(job: Path) -> dict[str, Any]:
    return json.loads(job.read_text())


def _write(job: Path, state: dict[str, Any]) -> None:
    tmp = job.with_name(job.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, job)


def alive(pid: int) -> bool:
    if pid <= 0:  # os.kill(0, 0) signals our own group, which always answers
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def progress(log: str) -> tuple[float, str]:
    """Progress from the job's log: the shots are the first 90 percent, the finish the rest."""
    if log.rstrip().endswith(WAITING):
        value, _ = progress(log.rstrip().removesuffix(WAITING))
        return value, "waiting for the GPU"
    if re.search(rf"^{FINISHING}", log, re.M):
        return 0.9, "finishing"
    if shots := _SHOT.findall(log):
        n, m, sid = shots[-1]
        return round(0.9 * (int(n) - 1) / int(m), 3), f"shot {n} of {m} ({sid})"
    return 0.0, "starting the render"


@mcp.tool(
    description="Render a Blender project's shots, then its finish step, into a video. Long job."
)
def blender_render(
    idempotency_key: str,
    output_path: str,
    project: str,
    quality: str = "draft",
    input_path: str = "",
    assets_path: str = "",
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`project` is the Blender project folder, a full path, holding `render.json`; `quality`
    one of its presets. A second call with the same key returns the same job unless it failed or
    its process died, which starts it again. It never installs: a render without Blender or
    ffmpeg is refused saying to reinstall."""
    output = Path(output_path)
    folder = project_folder(project)
    spec = load(folder)
    quality_of(spec, quality)
    job = _hidden(output, "job.json")
    answer = {"job_id": str(job), "poll": POLL, "progress": 0}
    if job.exists():
        state = _read(job)
        running = state["status"] == "running" and alive(state["pid"])
        if state["key"] == idempotency_key and (state["status"] == "done" or running):
            return {**answer, "message": "render already started"}
    blender()
    for name in ("ffmpeg", "ffprobe"):
        tool(name)
    output.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "key": idempotency_key,
        "status": "running",
        "project": str(folder),
        "quality": quality,
        "gpu": _gpu_here(),
        "assets": assets_path or assets_of(folder),
        "input": input_path,
        "output": str(output),
        "log": str(_hidden(output, "log")),
        "frontmatter": frontmatter or {},
        "started": _now(),
        "pid": 0,
    }
    _write(job, state)
    # start_new_session takes the render out of the MCP server's process group, which the
    # client kills when the session ends.
    pid = subprocess.Popen(
        [sys.executable, __file__, "--job", str(job)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).pid
    # ponytail: read-then-write, as in remotion_render; a lock file closes it if needed
    if _read(job)["status"] == "running":  # a near-instant render may already be done
        _write(job, {**state, "pid": pid})
    return {**answer, "message": "render started"}


@mcp.tool(description="Poll a Blender render started by blender_render.")
def blender_render_status(job_id: str) -> dict[str, Any]:
    job = Path(job_id)
    if not job.name.endswith(".job.json") or not job.is_file():
        raise ToolError(f"no render job at {job_id}")
    state = _read(job)
    if state["status"] == "done":
        return {
            "status": "done",
            "progress": 1,
            "output": state["output"],
            "sidecar": state["sidecar"],
            "bytes": state["bytes"],
        }
    if state["status"] == "running" and not alive(state["pid"]):
        state = _read(job)  # it may have finished between the two reads
        if state["status"] == "running":
            state = {**state, "status": "failed", "error": "the render process died"}
            _write(job, state)
    if state["status"] == "failed":
        return {"status": "failed", "error": f"{state['error']}\n{_tail(Path(state['log']))}"}
    log = Path(state["log"])
    value, message = progress(log.read_text() if log.exists() else "")
    return {"status": "running", "progress": value, "message": message}


def stamp(folder: Path, spec: dict[str, Any], entry: dict[str, Any], quality: str) -> str:
    """What a shot's frames depend on: its script, its entry and the quality, the project's
    size and frame rate, and the project's shared files (all but render.json, the shot scripts
    and what NOT_SHARED names)."""
    scripts = {(folder / s["script"]).resolve() for s in spec["shots"]}
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in NOT_SHARED)
        for name in sorted(files):
            path = Path(root) / name
            rel = path.relative_to(folder).as_posix()
            if rel == "render.json" or path.resolve() in scripts or name.endswith(".pyc"):
                continue
            with path.open("rb") as f:
                digest.update(f"{rel}\0".encode() + hashlib.file_digest(f, "sha256").digest())
    own = [entry, quality, spec.get("quality", {}).get(quality), spec["fps"]]
    own += [spec["width"], spec["height"], digest.hexdigest()]
    script = (folder / entry["script"]).read_bytes()
    return hashlib.sha256(json.dumps(own).encode() + script).hexdigest()


def _render(state: dict[str, Any], log: IO[str]) -> None:
    """The shots in order, each skipped when its stamp and frames are there, encoded to
    `<stage>/shots/<id>.mp4`; then the finish command, or the shots joined, into the output.
    Raises RuntimeError naming what failed."""
    folder, output, quality = Path(state["project"]), Path(state["output"]), state["quality"]
    spec = load(folder)
    stage = output.parents[1]  # <stage>/artifacts/<output>
    frames, shots, cache = stage / "frames", stage / "shots", stage / "shader-cache"
    shots.mkdir(parents=True, exist_ok=True)
    fps, size = str(spec["fps"]), f"{spec['width']}:{spec['height']}"
    if not state["gpu"]:
        log.write(f"{NO_GPU}\n")
    total = len(spec["shots"])
    for n, entry in enumerate(spec["shots"], 1):
        sid = entry["id"]
        log.write(f"Shot {n}/{total}: {sid}\n")
        log.flush()
        out, video, mark = frames / sid, shots / f"{sid}.mp4", frames / f"{sid}.stamp"
        now = stamp(folder, spec, entry, quality)
        done = mark.is_file() and mark.read_text() == now
        if done and video.is_file() and any(out.glob("frame_*.png")):
            log.write(f"Shot {sid} is up to date\n")
            continue
        mark.unlink(missing_ok=True)
        shutil.rmtree(out, ignore_errors=True)
        code = shot(folder, entry, quality, out, "", state["assets"], cache, log)
        if code != 0:
            raise RuntimeError(f"shot {sid}: Blender exited with {code}")
        if not any(out.glob("frame_*.png")):
            raise RuntimeError(f"shot {sid} wrote no frames")
        encode = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-framerate", fps]
        encode += ["-start_number", "1", "-i", str(out / "frame_%04d.png"), "-vf", f"scale={size}"]
        encode += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", fps, str(video)]
        if _run(encode, folder, dict(os.environ), log) != 0:
            raise RuntimeError(f"shot {sid}: ffmpeg could not encode its frames")
        mark.write_text(now)
    output.unlink(missing_ok=True)
    log.write(f"{FINISHING}\n")
    log.flush()
    if finish := spec.get("finish"):
        # ffmpeg, ffprobe and a python3 (this package's) first on PATH
        path = [str(Path(tool("ffmpeg")).parent), str(Path(sys.executable).parent)]
        env = {
            **os.environ,
            "PATH": os.pathsep.join([*path, os.environ.get("PATH", "")]),
            "BLENDER_PROJECT": str(folder),
            "BLENDER_ASSETS": state["assets"],
            "BLENDER_SHOTS": str(shots),
            "BLENDER_RUN": str(stage.parent),
            "BLENDER_OUTPUT": str(output),
        }
        code = _run(finish, folder, env, log)
        if code != 0:
            raise RuntimeError(f"the finish step exited with {code}")
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"the finish step wrote no video at {output}")
        return
    listed = shots / "join.txt"
    listed.write_text("".join(f"file '{shots / s['id']}.mp4'\n" for s in spec["shots"]))
    join = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat"]
    join += ["-safe", "0", "-i", str(listed), "-c", "copy", "-movflags", "+faststart", str(output)]
    if _run(join, folder, dict(os.environ), log) != 0:
        raise RuntimeError("ffmpeg could not join the shots")


def _sidecar(state: dict[str, Any], size: int) -> Path:
    output = Path(state["output"])
    fields: dict[str, Any] = {
        "type": "video",
        **state["frontmatter"],
        "created": _now(),
        "author": "blender_render",
        "file": output.name,
        "project": Path(state["project"]).name,
        "quality": state["quality"],
        "bytes": size,
    }
    head = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
    body = f"# {output.name}\n\nVideo rendered by Blender, quality {state['quality']}.\n"
    path = output.with_name(output.name + ".md")
    path.write_text(f"---\n{head}\n---\n\n{body}")
    return path


def _cancel(*_: Any) -> None:
    raise Cancelled


def run_job(job: Path) -> None:
    """The detached render, recorded done (with a sidecar) or failed. SIGTERM cancels it: the
    process it runs is killed and the job fails saying so."""
    signal.signal(signal.SIGTERM, _cancel)
    state = _read(job)
    output = Path(state["output"])
    with open(state["log"], "w") as log:
        try:
            _render(state, log)
        except Cancelled:
            state["error"] = "the render was cancelled"
        except (RuntimeError, ToolError, OSError) as exc:
            state["error"] = str(exc)
    if "error" not in state and output.is_file() and output.stat().st_size > 0:
        size = output.stat().st_size
        state = {**state, "status": "done", "bytes": size, "sidecar": str(_sidecar(state, size))}
    else:
        state = {"error": "the render wrote no video", **state, "status": "failed"}
    _write(job, {**state, "ended": _now()})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--job"]:
        run_job(Path(sys.argv[2]))
    else:
        mcp.run("stdio")
