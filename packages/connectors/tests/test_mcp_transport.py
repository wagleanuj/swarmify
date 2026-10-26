"""The MCP transport against a real connector process: the echo server over stdio and HTTP."""

import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

import echo_server
from swarmify_connectors import Connector, ConnectorError, Connectors, McpTransport

ECHO = str(Path(__file__).parent / "echo_server.py")


def echo(state: Path, credentials: list[str] | None = None) -> Connector:
    return Connector(
        "conn_1",
        "world_1",
        "echo",
        "stage",
        [sys.executable, ECHO, str(state)],
        credentials=credentials or [],
        stage_types=["connector"],
    )


def test_lists_the_tools_of_a_started_server(tmp_path: Path) -> None:
    tools = McpTransport().list_tools(echo(tmp_path), {})
    assert {t.name for t in tools} == {"echo", "env", "fail", "render", "render_status"}
    described = next(t for t in tools if t.name == "echo")
    assert described.description == "Answer with the text it was given."
    assert "text" in described.input_schema["properties"]


def test_credentials_reach_the_connector_process_and_the_api_environment_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARMIFY_LEAK_PROBE", "api-only")
    answer = McpTransport().call(echo(tmp_path), "env", {"name": "FAL_KEY"}, {"FAL_KEY": "k"})
    assert answer == {"value": "k", "leak": None}


def test_a_tool_error_and_a_missing_command_are_connector_errors(tmp_path: Path) -> None:
    transport = McpTransport()
    with pytest.raises(ConnectorError, match="said no"):
        transport.call(echo(tmp_path), "fail", {}, {})
    broken = Connector("c", "w", "broken", "stage", ["/nonexistent/swarmify-connector"])
    with pytest.raises(ConnectorError, match="broken"):
        transport.list_tools(broken, {})
    with pytest.raises(ConnectorError, match="no MCP server"):
        transport.list_tools(Connector("c", None, "graph", "tool"), {})


def test_a_server_that_never_answers_times_out(tmp_path: Path) -> None:
    sleeper = Connector(
        "c", "w", "sleepy", "stage", [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    started = time.monotonic()
    with pytest.raises(ConnectorError, match="did not answer within 0.5 s"):
        McpTransport(start_seconds=0.5).list_tools(sleeper, {})
    assert time.monotonic() - started < 10


def test_a_long_job_runs_to_done_across_sessions(tmp_path: Path, connectors: Connectors) -> None:
    connectors.transport = McpTransport()
    state = tmp_path / "jobs"
    state.mkdir()
    command = f"{sys.executable} {ECHO} {state}"
    connectors.add(connectors.draft("world_1", "echo", "stage", command, [], ["connector"]), {})
    stage: dict[str, Any] = {"calls": "echo", "output": "clip.mp4", "params": {"tool": "render"}}
    call = connectors.run_stage("world_1", "run_1", "stage_1", stage)
    assert (call.status, call.polls, call.job_id) == ("done", 2, "stage_1")
    assert call.response == {"status": "done", "progress": 1, "output": "clip.mp4"}


@pytest.fixture
def http_echo() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """The echo server over Streamable HTTP on localhost, recording request headers."""
    seen: list[dict[str, str]] = []
    inner = echo_server.mcp.streamable_http_app()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await inner(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp", seen
    server.should_exit = True
    thread.join(timeout=5)


def test_a_url_connector_gets_its_credentials_as_headers(
    http_echo: tuple[str, list[dict[str, str]]],
) -> None:
    url, seen = http_echo
    connector = Connector("c", "w", "remote", "stage", url=url, credentials=["API_KEY"])
    answer = McpTransport().call(connector, "echo", {"text": "hi"}, {"API_KEY": "k"})
    assert answer == {"text": "hi", "idempotency_key": ""}
    assert seen and all(h.get("api_key") == "k" for h in seen)
