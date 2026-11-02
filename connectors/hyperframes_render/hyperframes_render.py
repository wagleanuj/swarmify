"""The HyperFrames render connector: a stdio MCP server wrapping `hyperframes render` (ADR 0015),
the HyperFrames CLI the project itself installed (`node_modules/.bin/hyperframes`).

`hyperframes_render` starts one render per idempotency key and answers with a long job;
`hyperframes_render_status` reports its progress, read from the CLI's `@hf-progress` lines. The
render runs in a detached process (`--job <file>`) so it outlives the MCP session; its state, CLI
log and variables live beside the output as hidden files.

The composition receives the script as variables: `{...variables, "script": <text>,
"scriptPath": <path>}`, read with `window.__hyperframes.getVariables()`. On success the video gets
a markdown sidecar, `<output>.md`, with frontmatter (type video, world, run, stage, created) and a
body that names the source script, so the graph sees it, and the project's git commit when it has
one.

A relative `project` names a template in its templates folder, a Setting (`HYPERFRAMES_TEMPLATES`,
the data directory's `hyperframes/` unless changed). The Render workers Setting
(`SWARMIFY_HYPERFRAMES_WORKERS`) caps the CLI's workers. `check` answers the workflow check's rules
for a stage (the Check capability).

It is a Connector Package (ADR 0019): `swarmify-connector.toml` beside it is everything Swarmify
knows of it. Its install step puts the HyperFrames CLI, the Chrome Headless Shell it downloads,
ffmpeg and ffprobe in `node_modules/` here (ADR 0026), and every render uses them. It runs in the
connector sandbox (ADR 0021), which also holds the code a render executes, a template's or a
coding session's: it reads only this folder, its templates and the run's files, writes only its
stage's folder and its scratch folder, and reaches no host but its own loopback, so a render never
installs or downloads; a project brings its `node_modules`, scripts, fonts and images.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

POLL = "hyperframes_render_status"
WORKERS = "SWARMIFY_HYPERFRAMES_WORKERS"
TAIL = 40
_PROGRESS = re.compile(r"^@hf-progress (\{.*\})$", re.M)
PHASES = {
    "compile": "compiling",
    "extract_video": "extracting video frames",
    "process_audio": "processing audio",
    "start_browsers": "starting browsers",
    "capture": "capturing frames",
    "encode": "encoding",
    "assemble": "assembling",
}
"""The CLI's progress codes, in words."""
MODULES = Path(__file__).parent / "node_modules"
CLI = Path("node_modules") / ".bin" / "hyperframes"
"""The HyperFrames CLI in a project, which its `npm ci` installs."""
OFF = {
    "HYPERFRAMES_NO_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "HYPERFRAMES_NO_UPDATE_CHECK": "1",
    "HYPERFRAMES_NO_AUTO_INSTALL": "1",
    "HYPERFRAMES_NO_PREVIEW_DISCOVERY": "1",
}
"""Off in every render: telemetry, the update and skills checks (npm, GitHub), self-installs and
the look for a studio on port 5190, which the sandbox would refuse anyway (real render,
2026-11-02)."""
NAMES = {
    "HYPERFRAMES_BROWSER_PATH": "Chrome",
    "HYPERFRAMES_FFMPEG_PATH": "ffmpeg",
    "HYPERFRAMES_FFPROBE_PATH": "ffprobe",
}
"""What a render needs besides the CLI, by the variable that points HyperFrames at it."""

mcp = MCPServer("hyperframes_render")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_folder(project: str, templates: Path) -> Path:
    """A relative project is a template by name in the templates folder."""
    path = Path(project).expanduser()
    return path if path.is_absolute() else templates / path


def _templates() -> Path:
    default = Path.home() / ".swarmify" / "hyperframes"
    return Path(os.environ.get("HYPERFRAMES_TEMPLATES", default)).expanduser()


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
    """Progress from the CLI log: the last `@hf-progress` line's percent and phase."""
    for line in reversed(_PROGRESS.findall(log)):
        try:
            last = json.loads(line)
            code = str(last["code"])
        except (ValueError, TypeError, KeyError):
            continue  # a line cut off or odd: the one before it
        words = PHASES.get(code, code)
        if last.get("total") and last.get("done") is not None:
            words += f" {last['done']}/{last['total']}"
        return round(float(last.get("pct") or 0) / 100, 3), words
    return 0.0, "starting the render"


def _tail(log: Path) -> str:
    text = log.read_text(errors="replace") if log.exists() else ""
    return "\n".join(text.strip().splitlines()[-TAIL:])


def _flag(name: str, value: str) -> str:
    if not value or value.startswith("-"):
        raise ToolError(f"{name} must be a plain value, got {value!r}")
    return value


def _own(pattern: str) -> str | None:
    found = sorted(p for p in MODULES.glob(pattern) if p.is_file())
    return str(found[0]) if found else None


def tools() -> dict[str, str | None]:
    """Chrome Headless Shell, ffmpeg and ffprobe, by the variable that points HyperFrames at
    each: the ones the install step put beside it, else those on PATH, else None."""
    return {
        "HYPERFRAMES_BROWSER_PATH": _own(".hyperframes/chrome/**/chrome-headless-shell")
        or shutil.which("chrome-headless-shell"),
        "HYPERFRAMES_FFMPEG_PATH": _own(".ffmpeg/ffmpeg") or shutil.which("ffmpeg"),
        "HYPERFRAMES_FFPROBE_PATH": _own(".ffmpeg/ffprobe") or shutil.which("ffprobe"),
    }


@mcp.tool(description="Render a script artifact to a video with the HyperFrames CLI. Long job.")
def hyperframes_render(
    idempotency_key: str,
    output_path: str,
    project: str,
    composition: str = "index.html",
    quality: str = "",
    fps: str | int = "",
    input_path: str = "",
    variables: dict[str, Any] | None = None,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`project` is the HyperFrames project folder, or a template's name in the templates folder,
    and `composition` its file to render. A second
    call with the same key returns the same job unless it failed or its process died, which
    starts it again. It never installs, which the sandbox has no network for: a project without
    `node_modules`, or a render without Chrome, ffmpeg or ffprobe, is refused saying what to
    do."""
    if not input_path:
        raise ToolError("the render stage needs an `input`: the script artifact to render")
    output, script = Path(output_path), Path(input_path)
    folder = project_folder(project, _templates())
    if not script.is_file():
        raise ToolError(f"the script {script} does not exist")
    if not folder.is_dir():
        raise ToolError(f"the HyperFrames project {folder} does not exist")
    job = _hidden(output, "job.json")
    answer = {"job_id": str(job), "poll": POLL, "progress": 0}
    if job.exists():
        state = _read(job)
        running = state["status"] == "running" and alive(state["pid"])
        if state["key"] == idempotency_key and (state["status"] == "done" or running):
            return {**answer, "message": "render already started"}
    if not (folder / CLI).is_file():
        raise ToolError(f"{folder} has no node_modules: run npm ci there")
    found = tools()
    if missing := [NAMES[var] for var, path in found.items() if not path]:
        raise ToolError(f"no {', '.join(missing)}: reinstall hyperframes_render")
    # the project's own CLI: npx, which would find the same one, cannot in the sandbox on macOS
    argv = [str(folder / CLI), "render", str(folder)]
    argv += [f"--composition={_flag('composition', composition)}", f"--output={output}"]
    argv.append(f"--variables-file={_hidden(output, 'variables.json')}")
    if quality:
        argv.append(f"--quality={_flag('quality', quality)}")
    if fps:
        argv.append(f"--fps={_flag('fps', str(fps))}")
    # Chrome processes rendering at once, about 256 MB each. Unset, HyperFrames picks from the
    # CPUs and memory; a small Docker VM without a GPU wants 1 or 2.
    if workers := os.environ.get(WORKERS):
        argv.append(f"--workers={_flag('workers', workers)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    _hidden(output, "variables.json").write_text(
        json.dumps({**(variables or {}), "script": script.read_text(), "scriptPath": str(script)})
    )
    # HyperFrames' cache (fonts, extracted frames) in this process's scratch folder.
    env = {var: str(path) for var, path in found.items()} | OFF
    env["HYPERFRAMES_CACHE_DIR"] = str(Path(tempfile.gettempdir()) / "hyperframes")
    state = {
        "key": idempotency_key,
        "status": "running",
        "argv": argv,
        "env": env,
        "project": str(folder),
        "input": str(script),
        "output": str(output),
        "log": str(_hidden(output, "log")),
        "composition": composition,
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


@mcp.tool(description="Poll a HyperFrames render started by hyperframes_render.")
def hyperframes_render_status(job_id: str) -> dict[str, Any]:
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
    value, message = progress(Path(state["log"]).read_text() if Path(state["log"]).exists() else "")
    return {"status": "running", "progress": value, "message": message}


@mcp.tool(description="The workflow check's rule for a render stage: its project exists.")
def check(config: dict[str, Any]) -> dict[str, Any]:
    """`config` is the stage as its workflow writes it (calls, input, output, params). A
    missing project, input or output the core reports from the Manifest."""
    project = str((config.get("params") or {}).get("project") or "").strip()
    if not project or (folder := project_folder(project, _templates())).is_dir():
        return {"problems": []}
    where = "" if Path(project).expanduser().is_absolute() else f" in {folder.parent}"
    return {"problems": [f"the HyperFrames project {project} does not exist{where}"]}


def _sidecar(state: dict[str, Any], size: int) -> Path:
    output, script = Path(state["output"]), Path(state["input"])
    fields: dict[str, Any] = {
        "type": "video",
        **state["frontmatter"],
        "created": _now(),
        "author": "hyperframes_render",
        "file": output.name,
        "source": script.name,
        "composition": state["composition"],
        "bytes": size,
    }
    if commit := _commit(Path(state["project"])):
        fields["commit"] = commit
    head = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
    body = (
        f"# {output.name}\n\nVideo rendered by HyperFrames from the script `{script.name}` with"
        f" composition `{state['composition']}`, {size} bytes.\n"
    )
    path = output.with_name(output.name + ".md")
    path.write_text(f"---\n{head}\n---\n\n{body}")
    return path


def run_job(job: Path) -> None:
    """The detached render: run the CLI, then record done (with a sidecar) or failed. HyperFrames
    serves the project on a port the OS hands out, so no port is chosen here."""
    state = _read(job)
    output = Path(state["output"])
    with open(state["log"], "w") as log:
        try:
            ran = subprocess.run(
                state["argv"],
                cwd=state["project"],
                env={**os.environ, **state["env"]},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            code = ran.returncode
        except OSError as exc:
            code, state["error"] = -1, f"could not start hyperframes render: {exc}"
        if code != 0:
            state.setdefault("error", f"hyperframes render exited with {code}")
    if code == 0 and output.is_file() and output.stat().st_size > 0:
        size = output.stat().st_size
        state = {**state, "status": "done", "bytes": size, "sidecar": str(_sidecar(state, size))}
    else:
        state = {"error": "hyperframes render wrote no video", **state, "status": "failed"}
    _write(job, {**state, "ended": _now()})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--job"]:
        run_job(Path(sys.argv[2]))
    else:
        mcp.run("stdio")
