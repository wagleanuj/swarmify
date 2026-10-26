"""A model-free stand-in for graphify extract, shared by the API tests."""

import json
from pathlib import Path

from swarmify_memory import GRAPH_OUT

GRAPH = {
    "nodes": [{"id": "hooks", "label": "Short hooks"}, {"id": "cliff", "label": "Retention cliff"}],
    "links": [{"source": "hooks", "target": "cliff", "relation": "beats"}],
}


def fake_extract(folder: Path) -> None:
    """What graphify leaves behind, without a model: a two-node graph."""
    (folder / GRAPH_OUT).mkdir(exist_ok=True)
    (folder / GRAPH_OUT / "graph.json").write_text(json.dumps(GRAPH))
