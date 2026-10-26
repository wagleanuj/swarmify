"""The GraphIndex port of the Memory context (ADR 0012): read-only queries over a world's graph.

The real adapter is one graphify MCP process serving every world's graph (`McpGraphIndex`); the
fake answers from the graph file alone (`FakeGraphIndex`). A world's graph lives at
`<world folder>/graphify-out/graph.json` and only the GraphWriter writes it (ADR 0003).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

GRAPH_OUT = "graphify-out"
"""The graph output folder inside a world folder. Never input to its own rebuild."""
GRAPH_TOOLS = (
    "query_graph",
    "get_node",
    "get_neighbors",
    "god_nodes",
    "graph_stats",
    "shortest_path",
)
"""graphify's read-only tools a GraphIndex serves."""
EMPTY = "The world graph has nothing yet. Work from the task and the stage goal."


@dataclass(frozen=True)
class GodNode:
    """A node with unusually many connections."""

    label: str
    degree: int


def graph_file(folder: Path) -> Path:
    return folder / GRAPH_OUT / "graph.json"


def graph_size(folder: Path) -> tuple[int, int]:
    """(nodes, edges) of the world's graph; (0, 0) when it has none yet."""
    try:
        data = json.loads(graph_file(folder).read_text())
    except (OSError, ValueError):
        return 0, 0
    return len(data.get("nodes", [])), len(data.get("links", data.get("edges", [])))


class GraphIndex(Protocol):
    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        """Run one of GRAPH_TOOLS on the graph of the world at `folder`. Answers EMPTY when the
        world has no graph yet; raises ValueError for any other tool."""
        ...

    def god_nodes(self, folder: Path, top_n: int = 10) -> list[GodNode]:
        """Most connected first; empty when the world has no graph yet."""
        ...

    def close(self) -> None: ...
