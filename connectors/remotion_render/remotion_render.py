"""The Remotion render connector: a stdio MCP server wrapping `remotion render` (ADR 0015), the
Remotion CLI the project itself installed (`node_modules/.bin/remotion`).

`remotion_render` starts one render per idempotency key and answers with a long job;
`remotion_render_status` reports its progress, parsed from the CLI's `Rendered n/N` and
`Encoded n/N` lines. The render runs in a detached process (`--job <file>`) so it outlives the
MCP session; its state, CLI log and input props live beside the output as hidden files. On a GPU
it runs as steps (bundle once, then chunks of frames, each in a fresh browser, then one join),
since one long-lived tab runs out of WebGL contexts.

The composition receives the script as input props: `{"script": <text>, "scriptPath": <path>,
...props}`, read with `getInputProps()`. On success the video gets a markdown sidecar,
`<output>.md`, with frontmatter (type video, world, run, stage, created) and a body that names
the source script, so the graph sees it, and the template's git commit when it has one.

Its `project` is a folder Swarmify gives as a full path: the world's Template a direct render
stage names (ADR 0028), which the core resolves and checks, or a Build Session's build folder.

It is a Connector Package (ADR 0019): `swarmify-connector.toml` beside it is everything Swarmify
knows of it. Its install step puts the Remotion CLI and the Chrome Headless Shell it downloads in
`node_modules/` here (ADR 0026), and every render uses that browser. It runs in the connector
sandbox (ADR 0021), which also holds the code a render executes, a Template's or a coding
session's (`remotion.config.ts`, `node_modules`): it reads only this folder, the world's Remotion
Templates and the run's files, writes only its stage's folder, and reaches no host but its own
loopback, so a render never installs or downloads; a project brings its `node_modules`, fonts and
images, which a Template's prepare command installs.
"""

import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

POLL = "remotion_render_status"
CONCURRENCY = "SWARMIFY_RENDER_CONCURRENCY"
TAIL = 40
_STEP = re.compile(r"^(Rendered|Encoded) (\d+)/(\d+)", re.M)
_CHUNK = re.compile(r"^Chunk \d+-\d+: (\d+)/(\d+) frames done$", re.M)
CHUNK = 600
"""Frames each GPU render process draws before a fresh browser takes over: one tab rendering a
project that mounts a new 3D canvas per shot failed at frame 1661 creating a WebGL context."""
BROWSERS = Path(__file__).parent / "node_modules" / ".remotion"
CLI = Path("node_modules") / ".bin" / "remotion"
DEV = Path("/dev")
GPU = ("nvidia0", "dri/renderD*")
"""A GPU the sandbox handed this process (its Manifest's `permissions.gpu`): NVIDIA's, or any
driver's DRM render node."""
GPU_LOCK = "SWARMIFY_GPU_LOCK"
"""The lock file every process granted the GPU shares, which each GPU chunk holds."""
WAITING = "Waiting for the GPU"
GRACE = 5.0
"""Seconds a cancelled job has to stop on SIGTERM before it is killed."""
"""The Remotion CLI in a project, which its `npm ci` installs."""
"""Where the install step's `remotion browser ensure` put Chrome Headless Shell."""

mcp = MCPServer("remotion_render")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_folder(project: str) -> Path:
    """The project folder, which Swarmify gives as a full path: a world's Template it resolved
    from the stage's `project`, or a build folder of the run."""
    path = Path(project)
    if not path.is_absolute():
        raise ToolError(f"project must be a folder's full path, got {project!r}")
    return path


def _commit(folder: Path) -> str | None:
    try:
        git = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True
        )
    except OSError:  # no git, as in a slim image
        return None
    return git.stdout.strip() if git.returncode == 0 else None


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
    """Progress from the CLI log: rendering frames is the first 90 percent, encoding the rest.
    A render in chunks counts frames done over the whole range, after its last chunk line."""
    if log.rstrip().endswith(WAITING):
        value, _ = progress(log.rstrip().removesuffix(WAITING))
        return value, "waiting for the GPU"
    if chunks := list(_CHUNK.finditer(log)):
        before, whole = int(chunks[-1][1]), int(chunks[-1][2])
        steps = _STEP.findall(log, chunks[-1].end())
        word, done, total = steps[-1] if steps else ("Rendered", "0", "0")
        frames = min(before + int(done if word == "Rendered" else total), whole)
        return round(0.9 * frames / max(whole, 1), 3), f"rendered {frames}/{whole} frames"
    steps = _STEP.findall(log)
    if not steps:
        return 0.0, "starting the render"
    word, done, total = steps[-1]
    share = min(int(done) / max(int(total), 1), 1.0)
    value = 0.9 * share if word == "Rendered" else 0.9 + 0.1 * share
    return round(value, 3), f"{word.lower()} {done}/{total} frames"


def _tail(log: Path) -> str:
    text = log.read_text(errors="replace") if log.exists() else ""
    return "\n".join(text.strip().splitlines()[-TAIL:])


def _flag(name: str, value: str) -> str:
    if not value or value.startswith("-"):
        raise ToolError(f"{name} must be a plain value, got {value!r}")
    return value


@mcp.tool(description="Render a script artifact to a video with the Remotion CLI. Long job.")
def remotion_render(
    idempotency_key: str,
    output_path: str,
    project: str,
    composition: str,
    entry: str = "src/index.ts",
    codec: str = "h264",
    frames: str | int = "",
    input_path: str = "",
    props: dict[str, Any] | None = None,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`project` is the Remotion project folder, a full path;
    `entry` its entry point relative to it, and `frames` an optional range like `0-29`. A second
    call with the same key returns the same job unless it failed or its process died, which
    starts it again."""
    if not input_path:
        raise ToolError("the render stage needs an `input`: the script artifact to render")
    output, script = Path(output_path), Path(input_path)
    folder = project_folder(project)
    if not script.is_file():
        raise ToolError(f"the script {script} does not exist")
    if not folder.is_dir():
        raise ToolError(f"the Remotion project {folder} does not exist")
    job = _hidden(output, "job.json")
    answer = {"job_id": str(job), "poll": POLL, "progress": 0}
    if job.exists():
        state = _read(job)
        running = state["status"] == "running" and alive(state["pid"])
        if state["key"] == idempotency_key and (state["status"] == "done" or running):
            return {**answer, "message": "render already started"}
    # the project's own CLI: npx, which would find the same one, cannot in the sandbox on macOS
    argv = [str(folder / CLI), "render", _flag("entry", entry)]
    argv += [_flag("composition", composition), str(output)]
    argv += [f"--props={_hidden(output, 'props.json')}", f"--codec={_flag('codec', codec)}"]
    if frames:
        argv.append(f"--frames={_flag('frames', str(frames))}")
    # Tabs rendering at once. Unset, Remotion uses half the CPUs; with no GPU (Docker) a 3D
    # scene renders in software and that many tabs run out of memory (spike 2026-10-30). On the
    # GPU the browser flags fix it at one tab, and the Setting does not apply: the job renders
    # there in chunks (_chunked), from these arguments.
    browser = _browser()
    tabs = os.environ.get(CONCURRENCY)
    if tabs and not any(a.startswith("--concurrency=") for a in browser):
        argv.append(f"--concurrency={_flag('concurrency', tabs)}")
    argv += browser
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    _hidden(output, "props.json").write_text(
        json.dumps({**(props or {}), "script": script.read_text(), "scriptPath": str(script)})
    )
    state = {
        "key": idempotency_key,
        "status": "running",
        "argv": argv,
        "project": str(folder),
        "input": str(script),
        "output": str(output),
        "log": str(_hidden(output, "log")),
        "composition": composition,
        # ponytail: only mp4 codecs render in chunks, the ones whose audio joins as AAC
        "chunks": "--gl=angle-egl" in browser and codec in ("h264", "h265"),
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
    # ponytail: read-then-write, so a job that finishes in the microseconds between could still
    # be overwritten; a lock file closes it if a real render ever ends that fast.
    if _read(job)["status"] == "running":  # a near-instant render may already be done
        _write(job, {**state, "pid": pid})
    return {**answer, "message": "render started"}


def stop(pid: int) -> None:
    """Stop a job: SIGTERM to its process, then, once it is gone or GRACE seconds pass, SIGKILL
    to its process group, so nothing it started (or a job that ignored SIGTERM) runs on."""
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.monotonic() + GRACE
    while not _gone(pid) and time.monotonic() < end:
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _gone(pid: int) -> bool:
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid  # this process started it: reap it
    except ChildProcessError:
        return not alive(pid)


@mcp.tool(description="Poll a Remotion render started by remotion_render; cancel: true stops it.")
def remotion_render_status(job_id: str, cancel: bool = False) -> dict[str, Any]:
    """`cancel` stops a running render (`stop`), as when its run is aborted; the job fails
    saying it was cancelled."""
    job = Path(job_id)
    if not job.name.endswith(".job.json") or not job.is_file():
        raise ToolError(f"no render job at {job_id}")
    state = _read(job)
    if cancel and state["status"] == "running":
        stop(state["pid"])
        state = _read(job)
        if state["status"] == "running":  # killed before it could say so
            state = {**state, "status": "failed", "error": "the render was cancelled"}
            _write(job, state)
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
    value, message = progress(Path(state["log"]).read_text() if Path(state["log"]).exists() else "")
    return {"status": "running", "progress": value, "message": message}


def _sidecar(state: dict[str, Any], size: int) -> Path:
    output, script = Path(state["output"]), Path(state["input"])
    fields: dict[str, Any] = {
        "type": "video",
        **state["frontmatter"],
        "created": _now(),
        "author": "remotion_render",
        "file": output.name,
        "source": script.name,
        "composition": state["composition"],
        "bytes": size,
    }
    if commit := _commit(Path(state["project"])):
        fields["commit"] = commit
    head = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
    body = (
        f"# {output.name}\n\nVideo rendered by Remotion from the script `{script.name}` with"
        f" composition `{state['composition']}`, {size} bytes.\n"
    )
    path = output.with_name(output.name + ".md")
    path.write_text(f"---\n{head}\n---\n\n{body}")
    return path


def _free_port() -> int:
    """Remotion's own port probe misses a server listening on IPv6 only and then renders that
    server's page; a port the OS just handed out is free on both."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _browser() -> list[str]:
    """The browser a render uses: Chrome for Testing drawing on the GPU (ANGLE on EGL) when the
    sandbox handed this process one and the install step downloaded it, else Chrome Headless
    Shell, which draws 3D in software, about 10x slower (measured 2026-11-03), else Remotion's
    own lookup, which fails offline. On the GPU it renders one tab at a time: tabs sharing ANGLE
    on EGL sometimes captured a frame of a new 3D canvas before it was drawn, a black or garbled
    frame at random cuts (four tabs, 2026-11-04), while one tab gave the same clean frames on
    every run, at 5.8 frames a second."""
    gpu = any(p.exists() for pattern in GPU for p in DEV.glob(pattern))
    tested = [p for p in BROWSERS.glob("chrome-for-testing/*/*/chrome") if p.is_file()]
    if gpu and tested:
        flags = ["--chrome-mode=chrome-for-testing", "--gl=angle-egl", "--concurrency=1"]
        return [f"--browser-executable={tested[0]}", *flags]
    return [f"--browser-executable={shell}"] if (shell := _shell()) else []


def _shell() -> str | None:
    """Chrome Headless Shell: the one the install step downloaded beside it, else one on PATH,
    else None. The binary is chrome-headless-shell, or headless_shell on Linux arm64."""
    names = ("chrome-headless-shell", "headless_shell")
    found = [p for p in BROWSERS.rglob("*") if p.name in names and p.is_file()]
    return str(found[0]) if found else shutil.which("chrome-headless-shell")


def _run(name: str, argv: list[str], project: Path, log: Any) -> str:
    """One CLI process, its output appended to the job's log and returned; RuntimeError when it
    fails, naming the step."""
    log.write(f"$ {' '.join(argv)}\n")
    log.flush()
    start = Path(log.name).stat().st_size
    try:
        code = subprocess.run(argv, cwd=project, stdout=log, stderr=subprocess.STDOUT).returncode
    except OSError as exc:
        raise RuntimeError(f"could not start {name}: {exc}") from exc
    if code != 0:
        raise RuntimeError(f"{name} exited with {code}")
    return Path(log.name).read_bytes()[start:].decode(errors="replace")


@contextmanager
def _gpu(log: Any) -> Iterator[None]:
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


def _frames(frames: str, composition: str, listed: Any) -> tuple[int, int]:
    """The first and last frame to render: the `frames` param's, else the composition's whole
    length, which `listed()` reads from `remotion compositions`' table (the CLI has no JSON)."""
    given = re.fullmatch(r"(\d+)(?:-(\d*))?", frames)
    if frames and not given:
        raise RuntimeError(f"frames must be a range like 0-29, got {frames!r}")
    if given and (given[2] or given.lastindex == 1):
        return int(given[1]), int(given[2] or given[1])
    row = re.search(rf"^{re.escape(composition)}\s.*?(\d+) \([\d.]+ sec\)\s*$", listed(), re.M)
    if not row:
        raise RuntimeError(f"remotion compositions listed no frame count for {composition}")
    return int(given[1]) if given else 0, int(row[1]) - 1


def _chunked(state: dict[str, Any], log: Any) -> None:
    """A GPU render as steps, each a fresh CLI process: bundle once; read the frame count; render
    CHUNK frames at a time from the bundle, each chunk writing its audio to a WAV of its own
    (`--separate-audio-to`, silent when the composition has none) so no process draws the whole
    length; then one `remotion ffmpeg` joins the parts and their audio. The hidden folders go
    either way."""
    output, project = Path(state["output"]), Path(state["project"])
    cli, entry, composition = state["argv"][0], state["argv"][2], state["argv"][3]
    flags = [a for a in state["argv"][5:] if not a.startswith("--frames=")]
    frames = next((a.split("=", 1)[1] for a in state["argv"] if a.startswith("--frames=")), "")
    bundle, parts = _hidden(output, "bundle"), _hidden(output, "parts")
    for folder in (bundle, parts):
        shutil.rmtree(folder, ignore_errors=True)
    parts.mkdir()
    try:
        _run("remotion bundle", [cli, "bundle", entry, f"--out-dir={bundle}"], project, log)
        port = f"--port={_free_port()}"
        listing = [cli, "compositions", str(bundle), *flags, port]
        first, last = _frames(
            frames, composition, lambda: _run("remotion compositions", listing, project, log)
        )
        starts = range(first, last + 1, CHUNK)
        for n, start in enumerate(starts):
            end = min(start + CHUNK - 1, last)
            log.write(f"Chunk {start}-{end}: {start - first}/{last - first + 1} frames done\n")
            argv = [cli, "render", str(bundle), composition, str(parts / f"{n}.mp4"), *flags]
            argv += [f"--frames={start}-{end}", f"--separate-audio-to={parts / f'{n}.wav'}"]
            argv.append(f"--port={_free_port()}")
            with _gpu(log):
                _run(f"remotion render of frames {start}-{end}", argv, project, log)
        log.write(f"Chunk {first}-{last}: {last - first + 1}/{last - first + 1} frames done\n")
        (parts / "video.txt").write_text("".join(f"file '{n}.mp4'\n" for n in range(len(starts))))
        (parts / "audio.txt").write_text("".join(f"file '{n}.wav'\n" for n in range(len(starts))))
        concat = ["-f", "concat", "-i"]
        join = [cli, "ffmpeg", "-hide_banner", "-y", *concat, str(parts / "video.txt")]
        join += [*concat, str(parts / "audio.txt"), "-map", "0:v", "-map", "1:a", "-c:v", "copy"]
        join += ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(output)]
        _run("remotion ffmpeg", join, project, log)
    finally:
        for folder in (bundle, parts):
            shutil.rmtree(folder, ignore_errors=True)


def run_job(job: Path) -> None:
    """The detached render: run the CLI (on a GPU, in chunks), then record done (with a sidecar)
    or failed. It never installs, which the sandbox has no network for: a project without
    `node_modules` or a render without Chrome Headless Shell fails saying what to do."""
    state = _read(job)
    output = Path(state["output"])
    project = Path(state["project"])
    render = [*state["argv"], f"--port={_free_port()}"]
    code = 0
    with open(state["log"], "w") as log:
        if not (project / CLI).is_file():
            code, state["error"] = -1, f"{project} has no node_modules: run npm ci there"
        elif not any(a.startswith("--browser-executable=") for a in render):
            code, state["error"] = -1, "no Chrome Headless Shell: reinstall remotion_render"
        elif state.get("chunks"):
            try:
                _chunked(state, log)
            except RuntimeError as exc:
                code, state["error"] = -1, str(exc)
        else:
            try:
                ran = subprocess.run(render, cwd=project, stdout=log, stderr=subprocess.STDOUT)
                code = ran.returncode
            except OSError as exc:
                code, state["error"] = -1, f"could not start remotion render: {exc}"
            if code != 0:
                state.setdefault("error", f"remotion render exited with {code}")
    if code == 0 and output.is_file() and output.stat().st_size > 0:
        size = output.stat().st_size
        state = {**state, "status": "done", "bytes": size, "sidecar": str(_sidecar(state, size))}
    else:
        state = {"error": "remotion render wrote no video", **state, "status": "failed"}
    _write(job, {**state, "ended": _now()})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--job"]:
        run_job(Path(sys.argv[2]))
    else:
        mcp.run("stdio")
