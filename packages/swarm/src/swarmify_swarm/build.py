"""The one kind of coding session Swarmify runs: a Build Session (ADR 0018), which a connector
package requests in its Manifest, supplying the system prompt, the brief, the starter files and the
network domains; the core knows nothing of what it builds.

Swarm agents have no shell. A build session does, inside its build folder only: the Agent SDK's OS
sandbox (Seatbelt on macOS, bubblewrap on Linux) runs every Bash command with no way out
(`allowUnsandboxedCommands: false`, `failIfUnavailable`), writes limited to the build folder and the
npm cache, reads of the data directory denied except the build folder, the npm cache and the stage's
own world (its read-only `assets/` included), and network limited to the domains the package
declares (`permissions.network`). The PreToolUse hooks enforce the same path rules on the file
tools, so a sandbox that is off or misconfigured still cannot write outside the folder or read the
data directory's `.env`; for Bash they refuse an unsandboxed command and, as a tripwire, one that
names the data directory or a `.env` file. The environment carries no connector keys: the
composition root blanks them, and the sandbox unsets them and the model credentials for every
command.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from swarmify_swarm.model import AgentRole
from swarmify_swarm.ports import Storage

TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
MODEL_CREDENTIALS = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
"""The CLI needs these to reach the model; its Bash commands do not."""
WRITES = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}
READS = {"Read": "file_path", "Glob": "path", "Grep": "path"}
_ENV = re.compile(r"(?<![\w.-])\.env(?![\w.-])")
ROLE: AgentRole = "worker"
AGENT = "builder"
MAX_TURNS, MINUTES = 80, 20.0
"""A build stage's defaults; its params `max_turns` and `minutes` override them."""


def readable(build: Path, world_folder: Path) -> list[Path]:
    """What the session may read besides its own folder: the world's assets and the run's other
    stage folders (the script, the narration takes, the decisions). Never a folder that holds the
    build folder: bubblewrap mounts a read-only path over the writable build folder inside it,
    and the sandbox then cannot start, so every command fails (Seatbelt lets the write win)."""
    stage = build.parent
    run = stage.parent
    others = sorted(d for d in run.iterdir() if d.is_dir() and d != stage) if run.is_dir() else []
    assets = world_folder / "assets"
    return ([assets] if assets.is_dir() else []) + others


def sandbox(
    build: Path,
    world_folder: Path,
    data_dir: Path,
    cache: Path,
    secrets: list[str],
    domains: Sequence[str] = (),
) -> dict[str, Any]:
    """The session's SandboxSettings. `secrets` are the connector key names to unset, `domains`
    the only hosts its Bash may reach (the package's `permissions.network`)."""
    build, world_folder, data_dir, cache = (
        p.resolve() for p in (build, world_folder, data_dir, cache)
    )
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": list(domains)},
        "filesystem": {
            "allowWrite": [str(build), str(cache)],
            "denyRead": [str(data_dir)],
            "allowRead": [str(cache), *map(str, readable(build, world_folder))],
        },
        "credentials": {
            "envVars": [{"name": n, "mode": "deny"} for n in [*secrets, *MODEL_CREDENTIALS]]
        },
    }


def _within(path: str, cwd: Path, *roots: Path) -> bool:
    """Resolved, so `..` and a symlink pointing out count as outside. No path is outside."""
    if not path:
        return False
    target = Path(path).expanduser()
    target = (target if target.is_absolute() else cwd / target).resolve()
    return any(target.is_relative_to(r) for r in roots)


def refusal(
    tool: str, input: dict[str, Any], build: Path, world_folder: Path, data_dir: Path
) -> str | None:
    """Why the build session may not make this call, by the path rules; None allows it.
    The Bash check is a tripwire on the command's text; the sandbox is the wall."""
    build, world_folder, data_dir = build.resolve(), world_folder.resolve(), data_dir.resolve()
    if tool in WRITES:
        if not _within(str(input.get(WRITES[tool]) or ""), build, build):
            return f"{tool} is limited to the build folder {build}."
    elif tool in READS:
        path = str(input.get(READS[tool]) or build)
        pattern = str(input.get("pattern") or "") if tool == "Glob" else ""
        if not _within(path, build, build, world_folder) or pattern.startswith(("/", "~", "..")):
            return f"{tool} is limited to the build folder and this world's folder."
        if _ENV.search(path):
            return "A .env file is never readable here."
    elif tool == "Bash":
        if input.get("dangerouslyDisableSandbox"):
            return "Bash runs only inside the sandbox."
        command = str(input.get("command") or "")
        for folder in (build, world_folder):  # inside the data directory, but readable
            command = command.replace(str(folder), "")
        names = {str(data_dir)}
        if data_dir.is_relative_to(Path.home()):
            names.add(f"~/{data_dir.relative_to(Path.home())}")
        if any(n in command for n in names) or _ENV.search(command):
            return "Bash may not name the data directory or a .env file."
    return None


def prompt(
    task: str,
    brief: str,
    decisions: list[str],
    inputs: Sequence[Path] = (),
    assets: Sequence[Path] = (),
) -> str:
    """The package's `task` for this stage, then what the core adds to every Build Session:
    the stage's other input files, the world's assets, the run's decisions and the operator's
    `brief`."""
    lines = [
        task,
        f"The stage's other inputs, also yours to read: {', '.join(map(str, inputs))}."
        if inputs
        else "",
        "The world's assets, read-only files the operator added (music beds, sound effects,"
        f" logos): {', '.join(map(str, assets))}."
        if assets
        else "",
        *decisions,
        f"The operator's brief: {brief}" if brief else "",
    ]
    return "\n".join(line for line in lines if line)


def last_lines(board: Storage, stage_id: str, n: int = 10) -> str:
    """The build agent's last log lines, for a failed stage's message."""
    entries = [e for e in board.list_log(stage_id) if e.agent == AGENT][-n:]
    return "\n".join(f"{e.kind}: {e.tool or ''} {e.detail}".strip() for e in entries)
