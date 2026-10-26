"""Agents of a swarm stage and their live log, through the Storage port on real SQLite."""

from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_swarm import Refused, SqliteStorage


def test_an_agent_is_added_once_and_updated_field_by_field(board: SqliteStorage) -> None:
    first = board.agent("world_1", "run_1", "stage_1", "worker-1", "worker")
    assert board.agent("world_1", "run_1", "stage_1", "worker-1", "worker").id == first.id
    assert (first.status, first.session_id, first.cost_usd, first.skills) == (
        "working",
        None,
        0.0,
        [],
    )
    board.update_agent("stage_1", "worker-1", session_id="s1", skills=["w:verify"])
    agent = board.update_agent("stage_1", "worker-1", cost_usd=0.5, status="done")
    assert (agent.status, agent.session_id, agent.cost_usd, agent.skills) == (
        "done",
        "s1",
        0.5,
        ["w:verify"],
    )
    assert board.list_agents("run_1") == [agent] and board.list_agents("run_2") == []
    # Each session's cost estimate adds up, a failed session's too.
    assert board.update_agent("stage_1", "worker-1", cost_usd=0.25).cost_usd == 0.75
    with pytest.raises(KeyError):
        board.update_agent("stage_1", "nobody", status="done")


def test_log_lines_keep_their_order_and_stream_as_events(board: SqliteStorage, db: Path) -> None:
    board.agent("world_1", "run_1", "stage_1", "worker-1", "worker")
    board.log("stage_1", "worker-1", "tool", tool="mcp__swarmify__query_graph", input={"q": "x"})
    board.log("stage_1", "worker-1", "denied", "not allowed", tool="Bash", input={})
    log = board.list_log("stage_1")
    assert [(e.kind, e.tool, e.input, e.detail) for e in log] == [
        ("tool", "mcp__swarmify__query_graph", {"q": "x"}, ""),
        ("denied", "Bash", {}, "not allowed"),
    ]
    with pytest.raises(KeyError):
        board.log("stage_1", "nobody", "tool")
    with closing(connect(db)) as conn:
        events = read_after(conn, "world_1", 0)
    assert [e.type for e in events] == ["agent.updated", "agent.logged", "agent.logged"]
    assert events[-1].payload["detail"] == "not allowed" and events[-1].run_id == "run_1"


def test_deleting_a_world_drops_its_agents_and_log(board: SqliteStorage) -> None:
    board.agent("world_1", "run_1", "stage_1", "worker-1", "worker")
    board.log("stage_1", "worker-1", "session", "started")
    board.delete_world("world_1")
    assert board.list_agents("run_1") == [] and board.list_log("stage_1") == []


def test_a_verdict_numbers_its_loop_and_adds_its_gap_tasks_at_once(
    board: SqliteStorage, db: Path
) -> None:
    common = {"items": [], "path": "runs/run_1/stage_1/artifacts/verdict.md", "added_by": "judge"}
    first = board.record_verdict(
        "world_1", "run_1", "stage_1", passed=False, reasons=["thin"], gaps=["A", "B"], **common
    )
    second = board.record_verdict(
        "world_1", "run_1", "stage_1", passed=True, reasons=["ok"], gaps=[], **common
    )
    assert (first.loop, second.loop) == (1, 2)
    assert board.list_verdicts("stage_1") == [first, second]
    assert [(t.title, t.added_by) for t in board.list_tasks("stage_1")] == [
        ("A", "judge"),
        ("B", "judge"),
    ]
    with closing(connect(db)) as conn:
        types = [e.type for e in read_after(conn, "world_1", 0)]
    assert types == ["task.updated", "task.updated", "verdict.created", "verdict.created"]
    with pytest.raises(Refused):  # a gap without a title rolls back the verdict too
        board.record_verdict(
            "world_1", "run_1", "stage_1", passed=False, reasons=["r"], gaps=[" "], **common
        )
    assert len(board.list_verdicts("stage_1")) == 2
    board.delete_world("world_1")
    assert board.list_verdicts("stage_1") == []


def test_a_seed_lands_whole_with_its_dependencies(board: SqliteStorage) -> None:
    a, b = board.add_tasks(
        "world_1", "run_1", "stage_1", [("A", "", ()), ("B", "b", (0,))], added_by="planner"
    )
    assert b.depends_on == [a.id] and a.added_by == "planner"
    with pytest.raises(Refused):
        board.add_tasks("world_1", "run_1", "stage_1", [("C", "", ()), ("", "", ())], added_by="p")
    assert len(board.list_tasks("stage_1")) == 2


def test_migration_11_keeps_agents_and_their_log(tmp_path: Path) -> None:
    from swarmify_kernel import MIGRATIONS as KERNEL
    from swarmify_kernel.sqlite import migrate
    from swarmify_swarm import MIGRATIONS

    old = tmp_path / "old"
    old.mkdir()
    for f in MIGRATIONS.glob("*.sql"):
        if not f.name.startswith("0011"):
            (old / f.name).write_text(f.read_text())
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL, old)
    board = SqliteStorage(db)
    board.agent("world_1", "run_1", "stage_1", "worker-1", "worker")
    board.log("stage_1", "worker-1", "tool", tool="Read")
    with closing(connect(db)) as conn:
        assert migrate(conn, KERNEL, MIGRATIONS) == [11]
    assert [a.name for a in board.list_agents("run_1")] == ["worker-1"]
    assert [e.tool for e in board.list_log("stage_1")] == ["Read"]
    board.agent("world_1", "run_1", "stage_1", "judge", "judge")
    assert board.log("stage_1", "judge", "session").seq == 2
    board.delete_world("world_1")
    assert board.list_log("stage_1") == []
