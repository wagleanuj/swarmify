"""FakeGraphIndex: answers from the graph file alone, records every call, starts no process."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from swarmify_memory.ports import EMPTY, GRAPH_TOOLS, GodNode, graph_file


class FakeGraphIndex:
    def __init__(self, answer: str = "Short hooks win.") -> None:
        self.answer = answer
        self.calls: list[tuple[Path, str, dict[str, Any]]] = []

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        if tool not in GRAPH_TOOLS:
            raise ValueError(f"{tool} is not a graph tool")
        self.calls.append((folder, tool, args))
        return self.answer if graph_file(folder).exists() else EMPTY

    def god_nodes(self, folder: Path, top_n: int = 10) -> list[GodNode]:
        try:
            data = json.loads(graph_file(folder).read_text())
        except (OSError, ValueError):
            return []
        labels = {n["id"]: n.get("label", n["id"]) for n in data.get("nodes", [])}
        degree = Counter(e[end] for e in data.get("links", []) for end in ("source", "target"))
        return [GodNode(labels.get(i, i), d) for i, d in degree.most_common(top_n)]

    def close(self) -> None:
        pass
