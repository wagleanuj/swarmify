"""compose.yaml checks required variables per service, so one service can start alone."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEEDS = ROOT / "docker" / "needs.sh"


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(NEEDS), *args],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
    )


def test_needs_refuses_while_a_variable_is_empty_and_names_it() -> None:
    out = run({"A": "set"}, "A", "B", "--", "echo", "started")
    assert out.returncode == 1 and "B" in out.stderr and "started" not in out.stdout


def test_needs_starts_the_command_once_every_variable_is_set() -> None:
    out = run({"A": "1", "B": "2"}, "A", "B", "--", "echo", "started")
    assert out.returncode == 0 and out.stdout.strip() == "started"


def test_compose_has_no_file_wide_required_variables() -> None:
    # A `${VAR:?}` anywhere makes every `docker compose` command fail without VAR, Kokoro included.
    assert ":?" not in (ROOT / "compose.yaml").read_text()
