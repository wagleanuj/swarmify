"""Opt-in: real graphify with the Claude CLI backend over the fixture world, then queried through
the real GraphIndex. Needs a logged-in `claude` and spends subscription tokens, so it runs only
with SWARMIFY_GRAPHIFY=1, for example `SWARMIFY_GRAPHIFY=1 uv run pytest --no-cov -k live`."""

import json
import os
import shutil
from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import GRAPH_OUT, GraphWriter, McpGraphIndex

pytestmark = pytest.mark.skipif(
    os.environ.get("SWARMIFY_GRAPHIFY") != "1" or shutil.which("claude") is None,
    reason="set SWARMIFY_GRAPHIFY=1 with a logged-in claude CLI to run real graphify",
)
WORLD = Path(__file__).parent / "fixtures/world"


def test_real_graphify_builds_a_graph_the_index_can_query(tmp_path: Path) -> None:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    folder = tmp_path / "worlds/w1"
    shutil.copytree(WORLD, folder)
    (rebuilt,) = GraphWriter(db, tmp_path / "worlds", debounce=0).tick()
    assert rebuilt.type == "graph.rebuilt", rebuilt.payload
    assert rebuilt.payload["nodes"] > 0 and rebuilt.payload["nodes_added"] > 0
    nodes = json.loads((folder / GRAPH_OUT / "graph.json").read_text())["nodes"]
    assert not any(str(n.get("source_file", "")).startswith(GRAPH_OUT) for n in nodes)

    index = McpGraphIndex(tmp_path)
    try:
        answer = index.call(folder, "query_graph", {"question": "What keeps viewers watching?"})
        assert "retention" in answer.lower() or "hook" in answer.lower(), answer
        assert index.god_nodes(folder)
    finally:
        index.close()
