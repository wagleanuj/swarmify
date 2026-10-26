"""McpGraphIndex: the real GraphIndex, one `python -m graphify.serve` stdio process for every
world. graphify picks the graph per call from `project_path` and keeps the last eight loaded.

The process starts on the first query of a world that has a graph and lives on a private event
loop thread; calls from any thread are handed to that loop. A process that died is started again
on the next call.
"""

import asyncio
import logging
import re
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from swarmify_memory.ports import EMPTY, GRAPH_TOOLS, GodNode, graph_file

log = logging.getLogger(__name__)
_GOD = re.compile(r"^\s*\d+\.\s+(.+) - (\d+) edges?$")


def parse_god_nodes(text: str) -> list[GodNode]:
    """graphify's god_nodes answer, one `  1. Label - 12 edges` line per node."""
    return [GodNode(m[1], int(m[2])) for m in map(_GOD.match, text.splitlines()) if m]


class McpGraphIndex:
    def __init__(self, cwd: Path, start_seconds: float = 60.0, call_seconds: float = 60.0) -> None:
        """`cwd` must hold no graphify-out of its own, so the server starts with no default
        graph and serves only the graphs callers name."""
        self.cwd = cwd
        self.start_seconds = start_seconds
        self.call_seconds = call_seconds
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        if tool not in GRAPH_TOOLS:
            raise ValueError(f"{tool} is not a graph tool")
        if not graph_file(folder).exists():
            return EMPTY
        client, loop = self._ensure()
        arguments = {**args, "project_path": str(folder.resolve())}
        future = asyncio.run_coroutine_threadsafe(client.call_tool(tool, arguments), loop)
        result = future.result(self.call_seconds)
        text = "\n".join(getattr(b, "text", "") for b in result.content).strip()
        return f"Error: {text}" if result.is_error else text

    def god_nodes(self, folder: Path, top_n: int = 10) -> list[GodNode]:
        if not graph_file(folder).exists():
            return []
        return parse_god_nodes(self.call(folder, "god_nodes", {"top_n": top_n}))

    def close(self) -> None:
        with self._lock:
            loop, stop, thread = self._loop, self._stop, self._thread
            self._client = None
        if loop and stop and thread and thread.is_alive():
            loop.call_soon_threadsafe(stop.set)
            thread.join(10)

    def _ensure(self) -> tuple[Client, asyncio.AbstractEventLoop]:
        with self._lock:
            if self._client and self._loop and self._thread and self._thread.is_alive():
                return self._client, self._loop
            ready: Future[None] = Future()
            self._thread = threading.Thread(target=self._serve, args=(ready,), daemon=True)
            self._thread.start()
            ready.result(self.start_seconds)
            assert self._client and self._loop
            return self._client, self._loop

    def _serve(self, ready: "Future[None]") -> None:
        async def main() -> None:
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "graphify.serve"], cwd=str(self.cwd)
            )
            try:
                async with Client(params, cache=None) as client:
                    self._loop, self._client = asyncio.get_running_loop(), client
                    self._stop = asyncio.Event()
                    ready.set_result(None)
                    await self._stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise
            finally:
                self._client = None

        try:
            asyncio.run(main())
        except Exception:
            log.exception("the graphify MCP server stopped; the next call starts a new one")
