"""Telling a network outage from a failed session, and waiting for the network with backoff."""

import asyncio
import socket
from pathlib import Path

import pytest
from claude_agent_sdk import CLIConnectionError, ProcessError

from swarmify_swarm import SdkRuntime
from swarmify_swarm.fake import Answer, Die, Say, Script
from swarmify_swarm.network import NetworkDown, NetworkWait, Patient, transient
from swarmify_swarm.ports import SessionInit, SessionSpec


class APIConnectionError(Exception):
    """Named like the Anthropic client's, which is matched by name."""


@pytest.mark.parametrize(
    "error",
    [
        "API Error: Can't reach the API server (EAI_AGAIN)",
        RuntimeError("getaddrinfo ENOTFOUND api.anthropic.com"),
        ProcessError("Command failed", 1, "read ECONNRESET"),
        "API Error: Connection error.",
        "API Error: Request timed out.",
        "connect ETIMEDOUT 160.79.104.10:443",
        ConnectionResetError("reset by peer"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
        APIConnectionError("Connection error."),
    ],
)
def test_a_network_error_is_transient(error: object) -> None:
    assert transient(error)  # type: ignore[arg-type]


def test_a_cause_that_is_a_network_error_makes_it_transient() -> None:
    try:
        try:
            raise OSError("EAI_AGAIN")
        except OSError as inner:
            raise RuntimeError("the session died") from inner
    except RuntimeError as exc:
        assert transient(exc)


@pytest.mark.parametrize(
    "error",
    [
        "tsc found 3 errors",
        RuntimeError("the claude process died"),
        "API Error: 400 invalid_request_error",
        CLIConnectionError("Claude Code not found"),
        None,
    ],
)
def test_a_task_or_agent_failure_is_not(error: object) -> None:
    assert not transient(error)  # type: ignore[arg-type]


def _wait(up_after: int, limit: float = 6 * 3600) -> tuple[NetworkWait, list[float]]:
    """A wait whose probe fails `up_after` times then reaches the API; its sleeps recorded."""
    probes, slept = iter([False] * up_after + [True] * 100), []
    return NetworkWait(limit=limit, probe=lambda: next(probes), sleep=slept.append, tick=1e9), slept


def test_the_wait_backs_off_from_30_seconds_doubling_to_5_minutes() -> None:
    wait, slept = _wait(up_after=6)
    assert wait.wait() == sum(slept)
    assert slept == [30, 60, 120, 240, 300, 300, 300]


def test_the_wait_gives_up_past_its_limit_counting_what_was_waited_before() -> None:
    wait, slept = _wait(up_after=1000, limit=3600)
    with pytest.raises(NetworkDown, match="1 h"):
        wait.wait(spent=600)
    assert 3000 <= sum(slept) < 3000 + 300


def test_a_stop_ends_the_wait_early() -> None:
    slept: list[float] = []
    wait = NetworkWait(probe=lambda: False, sleep=slept.append, tick=5)
    assert wait.wait(stop=lambda: len(slept) >= 2) == 10


class _Hooks:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def started(self, init: SessionInit) -> None:
        self.ids.append(init.session_id)

    def before_tool(self, tool: str, input: dict) -> str | None:
        return None

    def cost(self, usd: float) -> str | None:
        return None


SPEC = SessionSpec(system_prompt="s", prompt="Why?", cwd=Path("."), plugin=None, tools=[],
                   allowed=[], output_schema={"type": "object"})  # fmt: skip


def test_a_patient_runtime_waits_and_resumes_the_session_it_lost() -> None:
    offline = Die("API Error: Can't reach the API server (EAI_AGAIN)")
    script = Script([Say("Looking."), offline], [offline], [Answer({"reply": "Back."})])
    probes = iter([False, True, True])
    wait = NetworkWait(probe=lambda: next(probes), sleep=lambda _: None)
    hooks = _Hooks()
    end = asyncio.run(Patient(SdkRuntime(transport=script.transport), wait).run(SPEC, hooks))
    assert end.output == {"reply": "Back."}
    first, *again = script.sessions
    assert first.resume is None and [s.resume for s in again] == [hooks.ids[0]] * 2


def test_a_patient_runtime_passes_any_other_failure_on() -> None:
    wait = NetworkWait(probe=lambda: pytest.fail("no wait"), sleep=lambda _: None)
    runtime = Patient(SdkRuntime(transport=Script([Die()]).transport), wait)
    with pytest.raises(Exception, match="process died"):
        asyncio.run(runtime.run(SPEC, _Hooks()))
