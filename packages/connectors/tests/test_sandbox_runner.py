"""The sandbox runner (issue 109): a process the API starts runs in the runner's container as if
it ran in the API's, and the runner starts nothing but the sandbox's launcher and the Claude CLI."""

import asyncio
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from swarmify_connectors import runner
from swarmify_connectors.sandbox import Grants, Sandbox


@pytest.fixture
def socket_path() -> Iterator[str]:
    """A runner serving on a socket, its path short enough for one (108 bytes)."""
    folder = tempfile.mkdtemp(prefix="swarmify-runner-", dir="/tmp")
    path = f"{folder}/s.sock"
    threading.Thread(target=lambda: asyncio.run(runner.serve(path)), daemon=True).start()
    deadline = time.monotonic() + 10
    while not Path(path).exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield path


def through(path: str, argv: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "swarmify_connectors.runner", path, *argv]
    return subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=60)


def test_a_sandboxed_command_runs_through_the_runner_with_its_stdin_stdout_and_exit_code(
    socket_path: str, tmp_path: Path
) -> None:
    script = "import sys; print(sys.stdin.read().upper()); sys.exit(3)"
    confined = Sandbox(tmp_path).wrap([sys.executable, "-c", script], None, Grants())
    done = through(socket_path, confined.argv, "hello")
    assert (done.returncode, done.stdout.strip()) == (3, "HELLO"), done.stderr


def test_the_runner_starts_nothing_but_the_launcher(socket_path: str) -> None:
    done = through(socket_path, ["/bin/sh", "-c", "echo pwned"])
    assert done.returncode == 1 and "pwned" not in done.stdout
    assert "starts only the sandbox launcher and the Claude CLI" in done.stderr


def test_a_process_whose_caller_goes_away_is_stopped(socket_path: str, tmp_path: Path) -> None:
    ticks = tmp_path / "out" / "ticks"
    ticks.parent.mkdir()
    script = (
        f"import time\nwhile True:\n    open({str(ticks)!r}, 'a').write('.')\n    time.sleep(0.1)"
    )
    grants = Grants(writes=[ticks.parent])
    confined = Sandbox(tmp_path).wrap([sys.executable, "-c", script], None, grants)
    command = [sys.executable, "-m", "swarmify_connectors.runner", socket_path, *confined.argv]
    caller = subprocess.Popen(command, stdin=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while not ticks.exists() or len(ticks.read_text()) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    caller.kill()
    caller.wait()
    time.sleep(2)  # the launcher's grace for late denials, then the sandbox is gone
    before = len(ticks.read_text())
    time.sleep(1)
    assert len(ticks.read_text()) == before
