"""The MCP adapter of ConnectorTransport: one short client session per list or call, or one
`session` a connector stage keeps for its call and the polls of its long job.

A command is launched over stdio with the connector's credentials merged over the MCP SDK's safe
default environment (HOME, PATH, USER and the like), so the API process's own environment never
reaches it. A URL is reached over Streamable HTTP with each credential sent as a header of the
same name. A long job resumed after a restart is polled in a new session, so a wrapper keeps job
state outside its process.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Any

import anyio
from anyio.from_thread import start_blocking_portal
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from swarmify_connectors.model import Connector, ConnectorError, Tool

Caller = Callable[[str, dict[str, Any]], dict[str, Any]]
"""Calls one tool of an open session: its name and arguments, its answer."""


@asynccontextmanager
async def _http(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as streams,
    ):
        yield streams


def _innermost(exc: BaseException) -> BaseException:
    """anyio task groups wrap the real error in exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _text(content: list[Any]) -> str:
    return "\n".join(getattr(block, "text", "") for block in content).strip()


class McpTransport:
    def __init__(self, start_seconds: float = 30.0) -> None:
        self.start_seconds = start_seconds

    def list_tools(self, connector: Connector, env: dict[str, str]) -> list[Tool]:
        async def go(client: Client) -> list[Tool]:
            listed = await client.list_tools()
            return [Tool(t.name, t.description or "", dict(t.input_schema)) for t in listed.tools]

        return self._run(connector, env, go, self.start_seconds)

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        # ponytail: no timeout on a call; a long job should answer with a job id and be polled.
        return self._run(connector, env, lambda client: _call(client, tool, arguments), None)

    @contextmanager
    def session(self, connector: Connector, env: dict[str, str]) -> Iterator[Caller]:
        """One server process for several calls, as a stage's call and its polls: the process
        lives as long as the job, so its sandbox (and the network it grants) does too."""
        with start_blocking_portal() as portal:
            with self._guard(connector, None):
                opened = portal.wrap_async_context_manager(self._client(connector, env))
                client = opened.__enter__()

            def caller(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                with self._guard(connector, None):
                    return portal.call(_call, client, tool, arguments)
                raise AssertionError("unreachable")  # pragma: no cover

            try:
                yield caller
            finally:
                with suppress(Exception):
                    opened.__exit__(None, None, None)

    def _client(self, connector: Connector, env: dict[str, str]) -> Client:
        server: Any
        if connector.command:
            command, *args = connector.command
            server = StdioServerParameters(
                command=command, args=args, env=env, cwd=connector.folder
            )
        elif connector.url:
            server = _http(connector.url, env)
        else:
            raise ConnectorError(f"{connector.name} has no MCP server to start")
        return Client(server, cache=None)

    @contextmanager
    def _guard(self, connector: Connector, seconds: float | None) -> Iterator[None]:
        """Every failure as a ConnectorError naming the connector."""
        try:
            yield
        except TimeoutError as exc:
            raise ConnectorError(f"{connector.name} did not answer within {seconds} s") from exc
        except ConnectorError:
            raise
        except Exception as exc:
            inner = _innermost(exc)
            if isinstance(inner, ConnectorError):
                raise inner from None
            raise ConnectorError(f"{connector.name}: {type(inner).__name__}: {inner}") from exc

    def _run[T](
        self,
        connector: Connector,
        env: dict[str, str],
        go: Callable[[Client], Awaitable[T]],
        seconds: float | None,
    ) -> T:
        client = self._client(connector, env)

        async def main() -> T:
            with anyio.fail_after(seconds):
                async with client:
                    return await go(client)

        with self._guard(connector, seconds):
            return anyio.run(main)
        raise AssertionError("unreachable")  # pragma: no cover


async def _call(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    text = _text(list(result.content))
    if result.is_error:
        raise ConnectorError(text or f"{tool} failed")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}
