"""The MCP adapter of ConnectorTransport: one short client session per list or call.

A command is launched over stdio with the connector's credentials merged over the MCP SDK's safe
default environment (HOME, PATH, USER and the like), so the API process's own environment never
reaches it. A URL is reached over Streamable HTTP with each credential sent as a header of the
same name. Long jobs are polled by the service across sessions, so a wrapper keeps job state
outside its process.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from swarmify_connectors.model import Connector, ConnectorError, Tool


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
        async def go(client: Client) -> dict[str, Any]:
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

        # ponytail: no timeout on a call; a long job should answer with a job id and be polled.
        return self._run(connector, env, go, None)

    def _run[T](
        self,
        connector: Connector,
        env: dict[str, str],
        go: Callable[[Client], Awaitable[T]],
        seconds: float | None,
    ) -> T:
        server: Any
        if connector.command:
            command, *args = connector.command
            server = StdioServerParameters(command=command, args=args, env=env)
        elif connector.url:
            server = _http(connector.url, env)
        else:
            raise ConnectorError(f"{connector.name} has no MCP server to start")

        async def main() -> T:
            with anyio.fail_after(seconds):
                async with Client(server, cache=None) as client:
                    return await go(client)

        try:
            return anyio.run(main)
        except TimeoutError as exc:
            raise ConnectorError(f"{connector.name} did not answer within {seconds} s") from exc
        except Exception as exc:
            inner = _innermost(exc)
            if isinstance(inner, ConnectorError):
                raise inner from None
            raise ConnectorError(f"{connector.name}: {type(inner).__name__}: {inner}") from exc
