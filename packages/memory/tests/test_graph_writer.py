"""The GraphWriter over real world folders with a fake extract and a hand-driven clock: it
debounces, skips its own output folder, runs one rebuild at a time and records each one."""

import shutil
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS, Event
from swarmify_kernel.events import append, read_after
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import GRAPH_OUT, GraphWriter, Memory, ShrinkRefused, extract_command
from swarmify_memory import writer as writer_module

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
        self.shrink = False
        self.seconds = seconds
        self.folders: list[str] = []
        self.allowed: list[bool] = []
        self.running = 0
        self.most = 0
        self.lock = threading.Lock()

    def __call__(self, folder: Path, allow_shrink: bool = False) -> dict[str, int]:
        self.allowed.append(allow_shrink)
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
            self.folders.append(folder.name)
        if self.fail:
            raise RuntimeError("claude is not logged in")
        if self.shrink and not allow_shrink:
            raise ShrinkRefused("unverified semantic shrink: 'a.md' (6 -> 5 nodes)")
        out = folder / GRAPH_OUT
        out.mkdir(exist_ok=True)
        shutil.copy(GRAPH, out / "graph.json")
        return {"input_tokens": 1000, "output_tokens": 200}


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


def test_a_world_deleted_mid_rebuild_or_while_queued_leaves_nothing_behind(
    db: Path, tmp_path: Path
) -> None:
    """Story 7: `a` is deleted while graphify runs on it, `b` while it waits its turn. The
    finished rebuild writes no event and no folder; the queued one never runs."""
    memory, worlds = Memory(db, tmp_path), tmp_path / "worlds"
    for world in ("a", "b"):
        memory.write_note(world, "r1", "s1", "worker-1", "Hooks", "Short ones.")
    fake = Extract()

    def extract(folder: Path, allow_shrink: bool) -> None:
        for world in ("a", "b"):  # what Worlds.delete does, while graphify still runs
            with closing(connect(db)) as conn:
                append(conn, Event.new("world.deleted", world_id=world, payload={"id": world}))
            shutil.rmtree(worlds / world)
        fake(folder)  # graphify recreates graphify-out in the deleted folder

    writer = GraphWriter(db, worlds, extract, debounce=0)
    assert writer.tick() == []
    assert fake.folders == ["a"]
    assert _events(db, "a") == _events(db, "b") == []
    assert not (worlds / "a").exists() and not (worlds / "b").exists()


def test_the_extract_command_names_the_claude_cli_backend_and_skips_its_output() -> None:
    cmd = extract_command(Path("/data/worlds/w1"))
    assert cmd[:4] == [sys.executable, "-m", "graphify", "extract"]
    assert cmd[4] == "/data/worlds/w1"
    assert cmd[cmd.index("--backend") + 1] == "claude-cli"
    assert cmd[cmd.index("--exclude") + 1] == f"{GRAPH_OUT}/"
    assert "--allow-partial" not in cmd
    assert "--allow-partial" in extract_command(Path("/data/worlds/w1"), allow_shrink=True)


def test_each_rebuild_records_its_extraction_time_and_tokens(db: Path, tmp_path: Path) -> None:
    memory, extract = Memory(db, tmp_path), Extract()
    memory.write_note("w1", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=0)
    (rebuilt,) = writer.tick()
    assert rebuilt.payload["input_tokens"] == 1000 and rebuilt.payload["output_tokens"] == 200
    assert rebuilt.payload["seconds"] >= 0

    extract.fail = True
    memory.write_note("w1", "r1", "s1", "worker-1", "Pacing", "Fast cuts.")
    (failed,) = writer.tick()
    assert failed.type == "graph.failed" and failed.payload["seconds"] >= 0
    assert failed.payload["shrink"] is False


def test_a_shrink_refusal_is_flagged_and_accepting_it_reruns_once_with_the_allow_flag(
    db: Path, tmp_path: Path
) -> None:
    memory, clock, extract = Memory(db, tmp_path), Clock(), Extract()
    extract.shrink = True
    memory.write_note("w1", "r1", "s1", "worker-1", "Hooks", "Short ones.")
    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=30, clock=clock)
    writer.tick()
    clock.now += 30
    (failed,) = writer.tick()
    assert failed.type == "graph.failed" and failed.payload["shrink"] is True
    assert "6 -> 5 nodes" in failed.payload["error"]

    accepted = writer.accept_shrink("w1")
    assert accepted.type == "graph.shrink_accepted"
    (rebuilt,) = writer.tick()  # at once, no debounce: the user asked for it
    assert rebuilt.type == "graph.rebuilt" and extract.allowed == [False, True]
    clock.now += 60
    assert writer.tick() == []  # the acceptance is spent

    # A writer started after the click (the external process, a restart) still honours it.
    extract.allowed.clear()
    memory.write_note("w1", "r1", "s1", "worker-1", "Pacing", "Fast cuts.")
    writer.tick()
    clock.now += 30
    assert [e.payload["shrink"] for e in writer.tick()] == [True]
    writer.accept_shrink("w1")
    fresh = GraphWriter(db, tmp_path / "worlds", extract, debounce=30, clock=clock)
    assert [e.type for e in fresh.tick()] == ["graph.rebuilt"]
    assert extract.allowed == [False, True]


def test_changes_while_a_world_waits_its_turn_are_absorbed_by_its_pending_rebuild(
    db: Path, tmp_path: Path
) -> None:
    memory, clock = Memory(db, tmp_path), Clock()
    for world in ("a", "b"):
        memory.write_note(world, "r1", "s1", "worker-1", "Hooks", "Short ones.")
    fake = Extract()

    def extract(folder: Path, allow_shrink: bool = False) -> dict[str, int]:
        if folder.name == "a":  # b changes while a rebuilds and b is still queued
            memory.write_note("b", "r1", "s1", "worker-1", "Pacing", "Fast cuts.")
        return fake(folder, allow_shrink)

    writer = GraphWriter(db, tmp_path / "worlds", extract, debounce=30, clock=clock)
    writer.tick()
    clock.now += 30
    assert [e.world_id for e in writer.tick()] == ["a", "b"]
    writer.tick()
    clock.now += 60
    assert writer.tick() == [] and fake.folders == ["a", "b"]


def test_run_graphify_reads_the_tokens_and_tells_a_shrink_refusal_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = subprocess.CompletedProcess([], 0, "[graphify extract] tokens: 12,345 in / 678 out", "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert writer_module.run_graphify(Path("/w")) == {"input_tokens": 12345, "output_tokens": 678}

    done = subprocess.CompletedProcess([], 0, "wrote graph.json", "")
    assert writer_module.run_graphify(Path("/w")) == {"input_tokens": 0, "output_tokens": 0}

    # graphify's own wording: a semantic shrink, then the refusal it leads to.
    refusal = (
        "[graphify extract] semantic extraction is incomplete: unverified semantic shrink detected"
        " for 'a.md' (6 -> 5 nodes). The shrink guard stays armed for this write; pass"
        " --allow-partial to overwrite a larger existing graph anyway.\n"
        "[graphify extract] error: extraction was incomplete (an AST/semantic pass failed) and the"
        " resulting graph is smaller than the existing graph.json. Refusing to overwrite a complete"
        " graph with a partial one. Re-run after fixing the failures, or pass --allow-partial to"
        " overwrite anyway."
    )
    done = subprocess.CompletedProcess([], 1, "", refusal)
    with pytest.raises(ShrinkRefused, match="allow-partial"):
        writer_module.run_graphify(Path("/w"))
    # Failed chunks name the same flag, but a plain retry heals them; accepting would write the
    # graph without those chunks' nodes.
    incomplete = (
        "[graphify extract] semantic extraction is incomplete: 0 dispatched file(s) produced no"
        " nodes and 2 came back truncated or hollow. The shrink guard stays armed for this write;"
        " pass --allow-partial to overwrite a larger existing graph anyway.\n"
        + refusal.splitlines()[1]
    )
    for stderr in [incomplete, refusal.splitlines()[1]]:
        done = subprocess.CompletedProcess([], 1, "", stderr)
        with pytest.raises(RuntimeError) as partial:
            writer_module.run_graphify(Path("/w"))
        assert not isinstance(partial.value, ShrinkRefused)
    done = subprocess.CompletedProcess([], 1, "", "claude: not logged in")
    with pytest.raises(RuntimeError) as other:
        writer_module.run_graphify(Path("/w"))
    assert not isinstance(other.value, ShrinkRefused)


def test_the_assets_are_files_not_knowledge_so_the_graph_skips_them(tmp_path: Path) -> None:
    cmd = extract_command(tmp_path)
    excluded = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--exclude"]
    assert excluded == [f"{GRAPH_OUT}/", "assets/"]
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/bed.mp3").write_bytes(b"ID3")
    (tmp_path / "notes.md").write_text("x")
    assert list(writer_module.snapshot(tmp_path)) == ["notes.md"]
