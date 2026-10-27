"""A world's Graph over HTTP: the GraphWriter rebuilds after notes land and says so on the
stream, the page's query box and god nodes read through the GraphIndex, and agents read only
the graphs their stage allows. The fake GraphIndex and a model-free extract sit behind it."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from sse import sse_events
from swarmify_api.main import create_app
from swarmify_memory import EMPTY, FakeGraphIndex
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

PLAN = [Answer({"tasks": [{"title": "Find 5 ideas."}]})]
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PASS = [Answer({"passed": True, "reasons": ["Five ideas."]})]


@pytest.fixture
def app(tmp_path: Path, script: Script, graph_index: FakeGraphIndex) -> FastAPI:
    """A GraphWriter that waits only a tenth of a second, so rebuilds happen within a test."""
    return create_app(
        tmp_path,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=graph_index,
        extract=fake_extract,
        graph_debounce=0.1,
        graph_poll=0.02,
        retro=False,
    )


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _until(check: Callable[[], object]) -> None:
    deadline = time.monotonic() + 15
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _research(graphs: str = "[]") -> str:
    return f"""name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    graphs: {graphs}
"""


def _run(client: TestClient, world: str, yaml: str) -> dict:
    wf = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
    run = client.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
    _until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    return client.get(f"/api/runs/{run['id']}").json()


def test_a_new_world_has_an_empty_graph(client: TestClient) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.get(f"/api/worlds/{world}/graph").json() == {
        "nodes": 0,
        "edges": 0,
        "god_nodes": [],
        "rebuilt": None,
        "failed": None,
    }
    assert client.get("/api/worlds/world_nope/graph").status_code == 404


def test_notes_from_a_run_are_rebuilt_into_the_graph_and_streamed(
    app: FastAPI, script: Script, tmp_path: Path
) -> None:
    script.scripts = [
        PLAN,
        [
            Call(tool_id("query_graph"), {"question": "What do we know?"}),
            Call(tool_id("claim_task")),
            Call(tool_id("write_note"), {"title": "Hooks", "body": "Short ones win."}),
            FINISH,
            Say("Done."),
        ],
        PASS,
    ]
    with TestClient(app) as client:
        world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
        assert _run(client, world, _research())["status"] == "done"
        _until(lambda: client.get(f"/api/worlds/{world}/graph").json()["rebuilt"])
        view = client.get(f"/api/worlds/{world}/graph").json()
    assert {k: view["rebuilt"][k] for k in ("nodes", "edges", "nodes_added", "edges_added")} == {
        "nodes": 2,
        "edges": 1,
        "nodes_added": 2,
        "edges_added": 1,
    }
    assert view["nodes"] == 2 and view["edges"] == 1 and view["failed"] is None
    assert view["god_nodes"][0] == {"label": "Short hooks", "degree": 1}
    assert (tmp_path / "worlds" / world / "graphify-out/graph.json").exists()


def test_rebuilt_events_reach_the_world_stream(live_url: str, tmp_path: Path) -> None:
    world = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()["id"]
    url = f"{live_url}/api/worlds/{world}/events"
    with httpx.stream("GET", url, headers={"Last-Event-ID": "1"}, timeout=10) as r:
        # The writer watches files, so a note dropped in by hand counts like an agent's.
        (tmp_path / "worlds" / world / "brief.md").write_text("# Brief\n\nShort hooks.\n")
        kind, _, data = next(sse_events(r))
    assert kind == "graph.rebuilt" and data["payload"]["nodes"] == 2


def test_the_query_box_asks_the_graph_index(
    client: TestClient, graph_index: FakeGraphIndex, tmp_path: Path
) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    url = f"/api/worlds/{world}/graph/query"
    assert client.post(url, json={"question": "What wins?"}).json() == {"answer": EMPTY}
    fake_extract(tmp_path / "worlds" / world)
    assert client.post(url, json={"question": " What wins? "}).json() == {
        "answer": "Short hooks win."
    }
    assert graph_index.calls[-1] == (
        tmp_path / "worlds" / world,
        "query_graph",
        {"question": "What wins?"},
    )
    assert client.post(url, json={"question": "  "}).status_code == 422
    assert (
        client.post("/api/worlds/world_nope/graph/query", json={"question": "x"}).status_code == 404
    )


def test_agents_read_their_world_and_listed_worlds_only_and_write_only_their_own(
    running: TestClient, script: Script, graph_index: FakeGraphIndex, tmp_path: Path
) -> None:
    video = running.post("/api/worlds", json={"name": "Video"}).json()["id"]
    market = running.post("/api/worlds", json={"name": "Market"}).json()["id"]
    secret = running.post("/api/worlds", json={"name": "Secret"}).json()["id"]
    script.scripts = [
        PLAN,
        [
            Call(tool_id("query_graph"), {"question": "What sells?", "world": "Market"}),
            Call(tool_id("god_nodes"), {}),
            Call(tool_id("graph_stats"), {"world": secret}),
            Call(tool_id("get_node"), {"label": "x", "project_path": str(tmp_path / "worlds")}),
            Call(tool_id("claim_task")),
            # Writing across worlds: tools take no world, and file tools are not allowed.
            Call(tool_id("write_note"), {"title": "Mine", "body": "x", "world": "Market"}),
            Call(
                tool_id("write_artifact"),
                {"name": "a.md", "content": "x", "world": market, "project_path": "../"},
            ),
            Call(
                "Write", {"file_path": str(tmp_path / "worlds" / market / "x.md"), "content": "x"}
            ),
            Call("Edit", {"file_path": str(tmp_path / "worlds" / market / "x.md")}),
            Call("Bash", {"command": f"touch {tmp_path / 'worlds' / market / 'x.md'}"}),
            FINISH,
            Say("Done."),
        ],
        PASS,
    ]
    assert _run(running, video, _research("[Market]"))["status"] == "done"
    worlds = tmp_path / "worlds"
    assert [p.name for p in (worlds / market).rglob("*") if p.is_file()] == []
    written = sorted(p.name for p in (worlds / video / "runs").rglob("*.md"))
    assert "mine.md" in written and "a.md" in written
    assert [(folder, tool) for folder, tool, _ in graph_index.calls] == [
        (worlds / market, "query_graph"),
        (worlds / video, "god_nodes"),
    ]
    texts = [(o.is_error, o.text.splitlines()[0]) for o in script.outcomes]
    assert texts[:2] == [(False, "[world Market]"), (False, "[world Video]")]
    assert all(err and "not readable" in text for err, text in texts[2:4]), texts
    assert [err for err, _ in texts[5:10]] == [False, False, True, True, True], texts
