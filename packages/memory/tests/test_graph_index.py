"""The real GraphIndex: one graphify MCP process answering for many worlds' graphs. The graphs
are fixture files, so no model runs."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from swarmify_memory import EMPTY, GRAPH_OUT, FakeGraphIndex, GodNode, McpGraphIndex, graph_size
from swarmify_memory.graph import parse_god_nodes

GRAPH = Path(__file__).parent / "fixtures/graph/graph.json"


def _world(tmp_path: Path, name: str, graph: bool = True) -> Path:
    folder = tmp_path / "worlds" / name
    (folder / GRAPH_OUT).mkdir(parents=True)
    if graph:
        shutil.copy(GRAPH, folder / GRAPH_OUT / "graph.json")
    return folder


@pytest.fixture
def index(tmp_path: Path) -> Iterator[McpGraphIndex]:
    index = McpGraphIndex(tmp_path)
    yield index
    index.close()


def test_one_process_serves_every_world_and_a_world_without_a_graph_is_empty(
    index: McpGraphIndex, tmp_path: Path
) -> None:
    video, market = _world(tmp_path, "video"), _world(tmp_path, "market")
    empty = _world(tmp_path, "empty", graph=False)

    answer = index.call(video, "query_graph", {"question": "what beats the retention cliff?"})
    assert "Short hooks" in answer and "Retention cliff" in answer
    assert "Nodes: 3" in index.call(market, "graph_stats", {})
    assert index.god_nodes(video, 2) == [GodNode("Retention cliff", 2), GodNode("Short hooks", 1)]
    assert index.call(empty, "query_graph", {"question": "anything?"}) == EMPTY
    assert index.god_nodes(empty) == []
    with pytest.raises(ValueError):
        index.call(video, "list_prs", {})

    index.close()  # a stopped process is started again on the next call
    assert "Short hooks" in index.call(video, "get_node", {"label": "Short hooks"})


def test_god_nodes_are_parsed_from_graphify_lines() -> None:
    text = (
        "God nodes (most connected):\n  1. Hook - question first - 71 edges\n  2. Pacing - 1 edge"
    )
    assert parse_god_nodes(text) == [GodNode("Hook - question first", 71), GodNode("Pacing", 1)]


def test_the_fake_answers_from_the_graph_file(tmp_path: Path) -> None:
    fake, video = FakeGraphIndex("Hooks win."), _world(tmp_path, "video")
    assert fake.call(video, "query_graph", {"question": "q"}) == "Hooks win."
    assert fake.call(_world(tmp_path, "none", graph=False), "god_nodes", {}) == EMPTY
    assert fake.god_nodes(video, 1) == [GodNode("Retention cliff", 2)]
    assert fake.god_nodes(tmp_path) == [] and graph_size(tmp_path) == (0, 0)
    assert graph_size(video) == (3, 2)
    with pytest.raises(ValueError):
        fake.call(video, "triage_prs", {})
