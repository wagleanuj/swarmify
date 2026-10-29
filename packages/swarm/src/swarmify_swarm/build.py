"""The one coding session Swarmify runs: a build stage writing a Remotion project (ADR 0018).

Swarm agents have no shell. A build session does, inside its build folder only: the Agent SDK's OS
sandbox (Seatbelt on macOS, bubblewrap on Linux) runs every Bash command with no way out
(`allowUnsandboxedCommands: false`, `failIfUnavailable`), writes limited to the build folder and the
npm cache, reads of the data directory denied except the build folder, the npm cache and the stage's
own world (its read-only `assets/` included), and network limited to the npm registry and Google
Fonts. The PreToolUse hooks enforce the same path rules on the file tools, so a sandbox that is off
or misconfigured still cannot write outside the folder or read the data directory's `.env`; for Bash
they refuse an unsandboxed command and, as a tripwire, one that names the data directory or a `.env`
file. The environment carries no connector keys: the composition root blanks them, and the sandbox
unsets them and the model credentials for every command.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from swarmify_swarm.model import AgentRole
from swarmify_swarm.ports import Storage

TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
DOMAINS = ["registry.npmjs.org", "fonts.googleapis.com", "fonts.gstatic.com"]
"""Where the sandboxed Bash may connect: npm installs and Google Fonts files."""
MODEL_CREDENTIALS = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
"""The CLI needs these to reach the model; its Bash commands do not."""
WRITES = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}
READS = {"Read": "file_path", "Glob": "path", "Grep": "path"}
_ENV = re.compile(r"(?<![\w.-])\.env(?![\w.-])")
ROLE: AgentRole = "worker"
AGENT = "builder"
MAX_TURNS, MINUTES = 80, 20.0
"""A build stage's defaults; its params `max_turns` and `minutes` override them."""


def sandbox(
    build: Path, world_folder: Path, data_dir: Path, cache: Path, secrets: list[str]
) -> dict[str, Any]:
    """The session's SandboxSettings. `secrets` are the connector key names to unset."""
    build, world_folder, data_dir, cache = (
        p.resolve() for p in (build, world_folder, data_dir, cache)
    )
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": DOMAINS},
        "filesystem": {
            "allowWrite": [str(build), str(cache)],
            "denyRead": [str(data_dir)],
            "allowRead": [str(build), str(cache), str(world_folder)],
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
    script: Path,
    composition: str,
    brief: str,
    decisions: list[str],
    inputs: Sequence[Path] = (),
    assets: Sequence[Path] = (),
) -> str:
    """`inputs` are the stage's other input files, beside the script; `assets` the world's."""
    lines = [
        f"Build the Remotion project in this folder (your working directory) so that"
        f" composition `{composition}` turns the script at {script} into a finished short video.",
        "Read the script first. The render passes it as input props: getInputProps() returns"
        " {script: <its text>, scriptPath: <its path>}, so the composition must read the script"
        " from there, not from a copied file.",
        "The stage's other inputs, also yours to read (copy what the render needs into public/):"
        f" {', '.join(map(str, inputs))}."
        if inputs
        else "",
        "The world's assets, read-only files the operator added (music beds, sound effects,"
        f" logos); copy what the video uses into public/: {', '.join(map(str, assets))}."
        if assets
        else "",
        *decisions,
        f"The operator's brief: {brief}" if brief else "",
    ]
    return "\n".join(line for line in lines if line)


SYSTEM = """You are the builder of Swarmify: you write a Remotion (React) project that renders a
short video. You have Bash, Read, Write, Edit, Glob and Grep, and everything you run is
sandboxed: you may write only inside the build folder, the network reaches only the npm registry
and Google Fonts, and anything else is refused. Do not try to get around a refusal.

How you work:
- The folder is seeded with a working project (package.json, src/index.ts registering a Root
  with the compositions). Keep src/index.ts as the entry point and keep the composition id you
  are asked for.
- Run `npm install` (add packages with `npm install <name>` if you need them), then write the
  composition. Check it compiles with `npx tsc --noEmit`.
- Do not render: Swarmify renders the composition when you finish, with no network. So every
  asset must be in the project: put images and fonts under public/ (download Google Fonts files
  with curl from fonts.gstatic.com and load them with @remotion/fonts or a CSS @font-face over
  staticFile()); @remotion/google-fonts would fetch at render time and fail.
- Finish with a one-paragraph summary of what the video shows."""


def last_lines(board: Storage, stage_id: str, n: int = 10) -> str:
    """The build agent's last log lines, for a failed stage's message."""
    entries = [e for e in board.list_log(stage_id) if e.agent == AGENT][-n:]
    return "\n".join(f"{e.kind}: {e.tool or ''} {e.detail}".strip() for e in entries)
