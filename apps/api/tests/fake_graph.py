"""A model-free stand-in for graphify extract, shared by the API tests."""

import json
from pathlib import Path

from swarmify_memory import GRAPH_OUT, ShrinkRefused

GRAPH = {
    "nodes": [{"id": "hooks", "label": "Short hooks"}, {"id": "cliff", "label": "Retention cliff"}],
    "links": [{"source": "hooks", "target": "cliff", "relation": "beats"}],
}


def fake_extract(folder: Path, allow_shrink: bool = False) -> dict[str, int]:
    """What graphify leaves behind, without a model: a two-node graph, and the tokens spent."""
    (folder / GRAPH_OUT).mkdir(exist_ok=True)
    (folder / GRAPH_OUT / "graph.json").write_text(json.dumps(GRAPH))
    return {"input_tokens": 1_000_000, "output_tokens": 100_000}


def shrinking_extract(folder: Path, allow_shrink: bool = False) -> dict[str, int]:
    """graphify whose shrink guard refuses every write it is not told to allow."""
    if not allow_shrink:
        raise ShrinkRefused("unverified semantic shrink for 'brief.md' (6 -> 5 nodes)")
    return fake_extract(folder)
