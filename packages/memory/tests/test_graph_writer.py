"""The GraphWriter over real world folders with a fake extract and a hand-driven clock: it
debounces, skips its own output folder, runs one rebuild at a time and records each one."""

import shutil
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import GRAPH_OUT, GraphWriter, Memory, extract_command

GRAPH = Path(__file__).parent / "fixtures/graph/graph.json"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Extract:
    """Writes the fixture graph, one more node each time, and records the order of calls."""

    def __init__(self, fail: bool = False, seconds: float = 0.0) -> None:
        self.fail = fail
        self.seconds = seconds
        self.folders: list[str] = []
        self.running = 0
        self.most = 0
        self.lock = threading.Lock()

    def __call__(self, folder: Path) -> None:
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
            self.folders.append(folder.name)
        if self.fail:
            raise RuntimeError("claude is not logged in")
        out = folder / GRAPH_OUT
        out.mkdir(exist_ok=True)
        shutil.copy(GRAPH, out / "graph.json")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    return db


def _events(db: Path, world: str) -> list[tuple[str, dict[str, object]]]:
    with closing(connect(db)) as conn:
        return [(e.type, e.payload) for e in read_after(conn, world, 0) if e.type.startswith("g")]


def test_a_burst_of_notes_is_one_rebuild_thirty_seconds_after_the_last(
    db: Path, tmp_path: Path
) -> None:
    memory, clock, extract = Memory(db, tmp_path), Clock(), Extract()
    (tmp_path / "worlds/w1").mkdir(parents=True)
    writer = GraphWriter(db, tmp_path / "worlds", extract, clock=clock)
    assert writer.tick() == []  # an empty world needs no graph

    memory.write_note("w1", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    writer.tick()
    clock.now += 20
    memory.write_note("w1", "r1", "s1", "worker-1", "Pacing", "Fast cuts.")
    writer.tick()
    clock.now += 29
    assert writer.tick() == [] and extract.folders == []  # 29 s after the last change
    clock.now += 1
    (rebuilt,) = writer.tick()

    assert extract.folders == ["w1"]
    assert rebuilt.type == "graph.rebuilt" and rebuilt.world_id == "w1"
    assert {k: rebuilt.payload[k] for k in ("nodes", "edges", "nodes_added", "edges_added")} == {
        "nodes": 3,
        "edges": 2,
        "nodes_added": 3,
        "edges_added": 2,
    }
    clock.now += 60
    assert writer.tick() == []  # its own graph-out writes are not changes
    assert [t for t, _ in _events(db, "w1")] == ["graph.rebuilt"]


def test_a_failed_rebuild_is_recorded_and_the_next_change_retries(db: Path, tmp_path: Path) -> None:
    memory, clock, extract = Memory(db, tmp_path), Clock(), Extract(fail=True)
    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=5, clock=clock)
    memory.write_note("w1", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    writer.tick()
    clock.now += 5
    (failed,) = writer.tick()
    assert failed.type == "graph.failed" and "not logged in" in failed.payload["error"]
    clock.now += 60
    assert writer.tick() == []

    extract.fail = False
    memory.write_note("w1", "r1", "s1", "worker-1", "Pacing", "Fast cuts.")
    writer.tick()
    clock.now += 5
    assert [e.type for e in writer.tick()] == ["graph.rebuilt"]


def test_a_world_with_files_newer_than_its_graph_is_rebuilt_at_start_up(
    db: Path, tmp_path: Path
) -> None:
    memory, clock, extract = Memory(db, tmp_path), Clock(), Extract()
    memory.write_note("stale", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    current = tmp_path / "worlds/current"
    memory.write_note("current", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    extract(current)  # its graph is newer than its notes
    extract.folders.clear()
    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=0, clock=clock)
    assert [e.world_id for e in writer.tick()] == ["stale"]


def test_rebuilds_run_one_at_a_time_across_worlds(db: Path, tmp_path: Path) -> None:
    memory, extract = Memory(db, tmp_path), Extract(seconds=0.05)
    for world in ("w1", "w2", "w3"):
        memory.write_note(world, "r1", "s1", "worker-1", "Hooks", "Short ones.")
    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=0)
    stop = threading.Event()
    thread = threading.Thread(target=writer.run, args=(stop, 0.01))
    thread.start()
    deadline = time.monotonic() + 5
    while len(extract.folders) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(5)
    assert sorted(extract.folders) == ["w1", "w2", "w3"] and extract.most == 1
    assert all(_events(db, w)[0][0] == "graph.rebuilt" for w in ("w1", "w2", "w3"))


def test_the_extract_command_names_the_claude_cli_backend_and_skips_its_output() -> None:
    cmd = extract_command(Path("/data/worlds/w1"))
    assert cmd[:4] == [sys.executable, "-m", "graphify", "extract"]
    assert cmd[4] == "/data/worlds/w1"
    assert cmd[cmd.index("--backend") + 1] == "claude-cli"
    assert cmd[cmd.index("--exclude") + 1] == f"{GRAPH_OUT}/"
