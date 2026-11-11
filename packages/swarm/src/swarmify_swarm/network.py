"""A session that died because the model API was unreachable (Wi-Fi down, DNS gone) is not a
failed attempt: whoever ran it waits for the network and resumes the same session. `transient`
tells such an error from an agent or task failure; `NetworkWait` waits, with backoff, until a
cheap probe reaches the API again, for at most `SWARMIFY_NETWORK_WAIT_HOURS` (6 by default)."""

import asyncio
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from swarmify_swarm.ports import AgentRuntime, SessionEnd, SessionHooks, SessionInit, SessionSpec

MARKERS = (
    "EAI_AGAIN",
    "ENOTFOUND",
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "ENETUNREACH",
    "ENETDOWN",
    "EHOSTUNREACH",
    "can't reach the api server",
    "api error: connection error",
    "api error: request timed out",
    "fetch failed",
    "socket hang up",
)
"""What the CLI and its HTTP client say when the API is out of reach, matched without case."""
TYPES = {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout"}
"""Connection error classes of the Anthropic and httpx clients, by name: neither is our import.
The Agent SDK's CLIConnectionError is not one: it means the local CLI, not the network."""
HOURS = 6.0
"""How long a session waits for the network by default; `SWARMIFY_NETWORK_WAIT_HOURS` sets it."""
NOTICE_AFTER = 600.0
"""Seconds of waiting after which the Needs-you page says so."""
ASKED = 120.0
"""How long a session someone waits on (the Run Assistant's turn) waits for the network."""


def transient(error: BaseException | str | None) -> bool:
    """True when `error` (an exception, its causes, or a session's error result) says the model
    API could not be reached, as opposed to the agent or its task failing."""
    texts, seen = [], error
    while isinstance(seen, BaseException):
        if isinstance(seen, ConnectionError | socket.gaierror) or type(seen).__name__ in TYPES:
            return True
        texts.append(str(seen))
        seen = seen.__cause__ or seen.__context__
    text = " ".join(texts) if texts else str(error or "")
    return any(m.lower() in text.lower() for m in MARKERS)


def reachable(timeout: float = 5.0) -> bool:
    """A DNS lookup and a TCP connect to the model API (`ANTHROPIC_BASE_URL`, else Anthropic's)."""
    url = urlsplit(os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
    try:
        socket.create_connection((url.hostname or "", url.port or 443), timeout).close()
        return True
    except OSError:
        return False


def _span(seconds: float) -> str:
    return f"{seconds / 3600:g} h" if seconds >= 3600 else f"{seconds / 60:g} min"


def _limit() -> float:
    return float(os.environ.get("SWARMIFY_NETWORK_WAIT_HOURS") or HOURS) * 3600


class NetworkDown(Exception):
    """The network did not come back within the wait's limit."""


@dataclass(frozen=True)
class Waiting:
    """An agent waiting for the network, since when, after which error."""

    world_id: str
    run_id: str
    stage_id: str
    agent: str
    since: datetime
    error: str


@dataclass
class NetworkWait:
    first: float = 30.0
    most: float = 300.0
    limit: float = field(default_factory=_limit)
    """Seconds one session may spend waiting in all."""
    probe: Callable[[], bool] = reachable
    sleep: Callable[[float], None] = time.sleep
    tick: float = 5.0
    """How often a sleep looks at `stop`, so a pause or an abort is not held up."""

    def wait(self, spent: float = 0.0, stop: Callable[[], object] = lambda: None) -> float:
        """Sleep `first` seconds, doubling up to `most`, until the probe reaches the API; the
        seconds slept. Returns early once `stop()` is truthy. Raises NetworkDown when `spent`
        (what the session waited before) plus this wait passes `limit`."""
        slept, delay = 0.0, self.first
        while True:
            end = slept + delay
            while slept < end:
                if stop():
                    return slept
                step = min(self.tick, end - slept)
                self.sleep(step)
                slept += step
            if self.probe():
                return slept
            if spent + slept >= self.limit:
                raise NetworkDown(f"the network was still down after {_span(self.limit)}")
            delay = min(delay * 2, self.most)


class _Seen:
    """Passes the hooks through, keeping the session id the runtime started."""

    def __init__(self, hooks: SessionHooks) -> None:
        self.hooks, self.session_id = hooks, None

    def started(self, init: SessionInit) -> None:
        self.session_id = init.session_id
        self.hooks.started(init)

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        return self.hooks.before_tool(tool, input)

    def cost(self, usd: float) -> str | None:
        return self.hooks.cost(usd)


class Patient:
    """An AgentRuntime that, when a session dies for want of the network, waits for it and
    resumes the same session; for sessions run outside a swarm stage (the Run Assistant), which
    keep no session ids of their own. Raises NetworkDown once `network` gives up."""

    def __init__(self, runtime: AgentRuntime, network: NetworkWait) -> None:
        self.runtime, self.network = runtime, network

    async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
        spent = 0.0
        while True:
            seen = _Seen(hooks)
            try:
                end = await self.runtime.run(spec, seen)
                if not (end.is_error and transient(end.result)):
                    return end
            except Exception as exc:
                if not transient(exc):
                    raise
            spent += await asyncio.to_thread(self.network.wait, spent)
            spec = replace(spec, resume=seen.session_id or spec.resume)
