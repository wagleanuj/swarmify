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


@dataclass(frozen=True)
class GraphNode:
    label: str
    source_file: str
    """The note or file it came from, relative to the world folder; empty for a concept."""
    run_id: str | None
    """The run that wrote that file, when a run did."""


@dataclass(frozen=True)
class Community:
    """A cluster graphify found, with its most connected nodes."""

    id: int
    name: str
    size: int
    nodes: list[GraphNode]


@dataclass(frozen=True)
class Surprise:
    """A connection graphify finds non-obvious: across files or across communities."""

    source: GraphNode
    target: GraphNode
    relation: str
    why: str


def _node(label: str, source_file: str) -> GraphNode:
    parts = source_file.split("/")
    run_id = parts[1] if len(parts) > 2 and parts[0] == "runs" else None
    return GraphNode(label, source_file, run_id)


def graph_map(
    folder: Path, communities: int = 8, per_community: int = 8, surprises: int = 5
) -> tuple[list[Community], list[Surprise]]:
    """The world graph's largest communities and graphify's surprising connections, read from
    the graph file; empty when the world has none yet."""
    import networkx as nx
    from graphify.analyze import surprising_connections
    from networkx.readwrite import json_graph

    try:
        data = json.loads(graph_file(folder).read_text())
        # A simple undirected graph, which graphify's analysis expects (a multigraph breaks it).
        graph = nx.Graph(json_graph.node_link_graph(data, edges="links"))
    except (OSError, ValueError, KeyError):
        return [], []
    # ponytail: parsed on every read, like graph_size; cache by mtime if graphs grow large.
    groups: dict[int, list[str]] = {}
    for member, attrs in graph.nodes(data=True):
        if attrs.get("community") is not None:
            groups.setdefault(int(attrs["community"]), []).append(member)

    def node(n: str) -> GraphNode:
        attrs = graph.nodes[n]
        return _node(str(attrs.get("label", n)), str(attrs.get("source_file") or ""))

    found = []
    for cid, members in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0]))[:communities]:
        top = sorted(members, key=lambda n: (-graph.degree(n), str(n)))[:per_community]
        name = graph.nodes[members[0]].get("community_name") or f"Community {cid}"
        found.append(Community(cid, str(name), len(members), [node(n) for n in top]))
    linked = [
        Surprise(
            _node(str(s["source"]), str(s["source_files"][0] or "")),
            _node(str(s["target"]), str(s["source_files"][1] or "")),
            str(s.get("relation", "")),
            str(s.get("why") or s.get("note") or ""),
        )
        for s in surprising_connections(graph, groups, surprises)
    ]
    return found, linked


class GraphIndex(Protocol):
    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        """Run one of GRAPH_TOOLS on the graph of the world at `folder`. Answers EMPTY when the
        world has no graph yet; raises ValueError for any other tool."""
        ...

    def god_nodes(self, folder: Path, top_n: int = 10) -> list[GodNode]:
        """Most connected first; empty when the world has no graph yet."""
        ...

    def close(self) -> None: ...
