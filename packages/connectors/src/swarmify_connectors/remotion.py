"""The Remotion render connector: a stdio MCP server wrapping `npx remotion render` (ADR 0015).

`remotion_render` starts one render per idempotency key and answers with a long job;
`remotion_render_status` reports its progress, parsed from the CLI's `Rendered n/N` and
`Encoded n/N` lines. The render runs in a detached process (`--job <file>`) so it outlives the
MCP session; its state, CLI log and input props live beside the output as hidden files.

The composition receives the script as input props: `{"script": <text>, "scriptPath": <path>,
...props}`, read with `getInputProps()`. On success the video gets a markdown sidecar,
`<output>.md`, with frontmatter (type video, world, run, stage, created) and a body that names
the source script, so the graph sees it, and the template's git commit when it has one.

A relative `project` names a template under `<data dir>/remotion/` (`SWARMIFY_DATA_DIR`, default
`~/.swarmify`); a template without `node_modules` gets `npm ci` (`npm install` without a
lockfile) once, in the render's log, before it renders.

Coverage reports this module low: the server and the detached job run in subprocesses the
coverage tool cannot see. The transport tests in tests/test_remotion.py exercise both.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

POLL = "remotion_render_status"
TAIL = 40
_STEP = re.compile(r"^(Rendered|Encoded) (\d+)/(\d+)", re.M)

mcp = MCPServer("remotion_render")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_folder(project: str, data_dir: Path) -> Path:
    """A relative project is a template by name under `<data dir>/remotion/`."""
    path = Path(project).expanduser()
    return path if path.is_absolute() else data_dir / "remotion" / path


def _data_dir() -> Path:
    return Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))


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
    """Progress from the CLI log: rendering frames is the first 90 percent, encoding the rest."""
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
    """`project` is the Remotion project folder, or a template's name under the data directory's
    `remotion/` folder; `entry` its entry point relative to it, and `frames` an optional range
    like `0-29`. A second call with the same key returns the same
    job unless it failed or its process died, which starts it again."""
    if not input_path:
        raise ToolError("the render stage needs an `input`: the script artifact to render")
    output, script = Path(output_path), Path(input_path)
    folder = project_folder(project, _data_dir())
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
    argv = ["npx", "--no", "remotion", "render", _flag("entry", entry)]
    argv += [_flag("composition", composition), str(output)]
    argv += [f"--props={_hidden(output, 'props.json')}", f"--codec={_flag('codec', codec)}"]
    if frames:
        argv.append(f"--frames={_flag('frames', str(frames))}")
    # The Docker image ships Chrome Headless Shell on PATH; elsewhere Remotion finds its own.
    if browser := shutil.which("chrome-headless-shell"):
        argv.append(f"--browser-executable={browser}")
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
        "frontmatter": frontmatter or {},
        "started": _now(),
        "pid": 0,
    }
    _write(job, state)
    # start_new_session takes the render out of the MCP server's process group, which the
    # client kills when the session ends.
    pid = subprocess.Popen(
        [sys.executable, "-m", "swarmify_connectors.remotion", "--job", str(job)],
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


@mcp.tool(description="Poll a Remotion render started by remotion_render.")
def remotion_render_status(job_id: str) -> dict[str, Any]:
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


def run_job(job: Path) -> None:
    """The detached render: run the CLI, then record done (with a sidecar) or failed."""
    state = _read(job)
    output = Path(state["output"])
    project = Path(state["project"])
    # ponytail: two first renders of one template at once both install; a lock if that bites.
    lock = (project / "package-lock.json").is_file()
    steps = [] if (project / "node_modules").is_dir() else [["npm", "ci" if lock else "install"]]
    code = 0
    with open(state["log"], "w") as log:
        for argv in [*steps, state["argv"]]:
            name = " ".join(argv[:2]) if argv[0] == "npm" else "remotion render"
            try:
                code = subprocess.run(
                    argv, cwd=project, stdout=log, stderr=subprocess.STDOUT
                ).returncode
            except OSError as exc:
                code, state["error"] = -1, f"could not start {name}: {exc}"
            if code != 0:
                state.setdefault("error", f"{name} exited with {code}")
                break
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
