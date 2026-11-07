"""Issue 332: a Build Session's Bash reads and writes its own build folder. The session's sandbox
rules (swarmify_swarm.build.sandbox) applied by sandbox-runtime, the library Claude Code's own
sandbox is, on this machine's OS sandbox (Seatbelt or bubblewrap): before the fix Seatbelt
refused `ls .` in the build folder while bubblewrap allowed it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmify_connectors.sandbox import LAUNCHER
from swarmify_swarm import build

RUNTIME = LAUNCHER.parent / "node_modules/@anthropic-ai/sandbox-runtime/dist/index.js"
SANDBOX = shutil.which("sandbox-exec") or shutil.which("bwrap")
RUN = """
import { spawnSync } from "node:child_process";
import { SandboxManager } from "%s";
const [rules, cwd, command] = JSON.parse(process.argv[1]);
const network = { allowedDomains: [], deniedDomains: [] };
const filesystem = { ...rules, denyWrite: [] };
await SandboxManager.initialize({ network, filesystem }, undefined, false);
const r = spawnSync("bash", ["-c", await SandboxManager.wrapWithSandbox(command)], { cwd });
process.stdout.write(r.stdout);
process.exit(r.status ?? 1);
"""


@pytest.mark.skipif(not (SANDBOX and shutil.which("node")), reason="no OS sandbox here")
def test_the_build_sessions_bash_reads_and_writes_its_build_folder_and_nothing_else(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    world = data / "worlds/w"
    folder, script = world / "runs/r/video/build", world / "runs/r/script/script.md"
    folder.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    (folder / "index.ts").write_text("starter\n")
    script.write_text("# Script\n")
    (data / ".env").write_text("KEY=1\n")
    rules = build.sandbox(folder, world, data, data / "npm-cache", [])["filesystem"]
    (data / "npm-cache").mkdir()

    def bash(command: str) -> subprocess.CompletedProcess[str]:
        args = json.dumps([rules, str(folder.resolve()), command])
        node = ["node", "--input-type=module", "-e", RUN % RUNTIME, args]
        return subprocess.run(node, capture_output=True, text=True, timeout=60)

    listed = bash("ls . && cat index.ts && echo x > made.txt && cat made.txt")
    assert listed.returncode == 0 and listed.stdout == "index.ts\nstarter\nx\n", listed
    assert bash(f"cat {script}").stdout == "# Script\n"  # the run's other stages
    assert bash(f"cat {data / '.env'}").returncode != 0
    assert bash(f"echo y > {script}").returncode != 0
    assert script.read_text() == "# Script\n"
