"""The whiteboard through its Storage port, against real SQLite in a temp folder."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_swarm import OPERATOR, Refused, SqliteStorage

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


def _add(board: SqliteStorage, title: str = "Pull top shorts", **kw: object) -> str:
    return board.add_task("world_1", "run_1", "stage_1", title, added_by=OPERATOR, **kw).id  # type: ignore[arg-type]


def test_a_task_is_added_open_with_who_added_it(board: SqliteStorage) -> None:
    task = board.add_task(
        "world_1", "run_1", "stage_1", "  Pull top shorts ", added_by=OPERATOR, body="Last 30 days."
    )
    assert (task.title, task.body, task.status, task.owner, task.added_by) == (
        "Pull top shorts",
        "Last 30 days.",
        "open",
        None,
        "operator",
    )
    assert task.heartbeat_at is None and task.depends_on == [] and task.notes == []
    assert board.get_task(task.id) == task
    assert board.list_tasks("stage_1") == [task]
    assert board.list_tasks("stage_2") == []
    with pytest.raises(Refused):
        _add(board, "   ")


def test_claim_heartbeat_finish(board: SqliteStorage) -> None:
    task_id = _add(board)
    claimed = board.claim(task_id, "gilfoyle", now=T0)
    assert (claimed.status, claimed.owner, claimed.heartbeat_at) == (
        "claimed",
        "gilfoyle",
        T0.isoformat(),
    )
    with pytest.raises(Refused, match="claimed by gilfoyle"):
        board.claim(task_id, "dinesh")
    with pytest.raises(Refused, match="dinesh does not hold"):
        board.heartbeat(task_id, "dinesh")

    later = T0 + timedelta(seconds=30)
    beat = board.heartbeat(task_id, "gilfoyle", note="Found 12 so far.", now=later)
    assert beat.heartbeat_at == later.isoformat()
    assert [(n.author, n.body) for n in beat.notes] == [("gilfoyle", "Found 12 so far.")]

    done = board.finish(task_id, "gilfoyle", note="Ranked all 30.")
    assert (done.status, done.owner, done.heartbeat_at) == ("done", "gilfoyle", None)
    assert [n.body for n in done.notes] == ["Found 12 so far.", "Ranked all 30."]
    with pytest.raises(Refused, match="done by gilfoyle"):
        board.claim(task_id, "dinesh")
    with pytest.raises(Refused):
        board.finish(task_id, "gilfoyle")


def test_unknown_tasks_are_key_errors(board: SqliteStorage) -> None:
    assert board.get_task("task_nope") is None
    with pytest.raises(KeyError):
        board.claim("task_nope", "gilfoyle")
    with pytest.raises(KeyError):
        board.heartbeat("task_nope", "gilfoyle")
    with pytest.raises(KeyError):
        board.finish("task_nope", "gilfoyle")


def test_a_task_is_ready_only_when_its_dependencies_are_done(board: SqliteStorage) -> None:
    pull = _add(board, "Pull top shorts")
    rank = _add(board, "Rank hooks", depends_on=(pull, pull))
    assert board.get_task(rank).depends_on == [pull]  # type: ignore[union-attr]
    with pytest.raises(Refused, match="blocked"):
        board.claim(rank, "dinesh")
    board.claim(pull, "gilfoyle")
    with pytest.raises(Refused, match="blocked"):
        board.claim(rank, "dinesh")
    board.finish(pull, "gilfoyle")
    assert board.claim(rank, "dinesh").owner == "dinesh"


def test_dependencies_stay_inside_the_stage(board: SqliteStorage) -> None:
    other = board.add_task("world_1", "run_1", "stage_2", "Elsewhere", added_by=OPERATOR).id
    with pytest.raises(Refused, match="not a task of this stage"):
        _add(board, depends_on=(other,))
    with pytest.raises(Refused):
        _add(board, depends_on=("task_nope",))
    assert board.list_tasks("stage_1") == []


def test_a_silent_claim_returns_to_open_with_its_partial_notes(board: SqliteStorage) -> None:
    quiet, busy = _add(board, "Quiet"), _add(board, "Busy")
    board.claim(quiet, "gilfoyle", now=T0)
    board.heartbeat(quiet, "gilfoyle", note="Halfway: 6 of 12 checked.", now=T0)
    board.claim(busy, "dinesh", now=T0)
    board.heartbeat(busy, "dinesh", now=T0 + timedelta(seconds=90))

    assert board.expire(now=T0 + timedelta(minutes=2)) == []  # exactly two minutes is not older
    reopened = board.expire(now=T0 + timedelta(minutes=2, seconds=1))
    assert [t.id for t in reopened] == [quiet]
    task = board.get_task(quiet)
    assert task is not None
    assert (task.status, task.owner, task.heartbeat_at) == ("open", None, None)
    assert [(n.author, n.body) for n in task.notes] == [
        ("gilfoyle", "Halfway: 6 of 12 checked."),
        ("swarmify", f"Returned to open: gilfoyle sent no heartbeat since {T0.isoformat()}."),
    ]
    assert board.get_task(busy).status == "claimed"  # type: ignore[union-attr]
    assert board.claim(quiet, "jared").owner == "jared"
    with pytest.raises(Refused):
        board.heartbeat(quiet, "gilfoyle")


def test_every_change_is_a_task_updated_event_carrying_the_task(
    board: SqliteStorage, db: Path
) -> None:
    task_id = _add(board)
    board.claim(task_id, "gilfoyle", now=T0)
    board.expire(now=T0 + timedelta(minutes=5))
    with pytest.raises(Refused):
        board.heartbeat(task_id, "gilfoyle")  # refused actions record nothing
    with closing(connect(db)) as conn:
        events = read_after(conn, "world_1", 0)
    assert [e.type for e in events] == ["task.updated"] * 3
    assert [e.payload["status"] for e in events] == ["open", "claimed", "open"]
    assert {e.run_id for e in events} == {"run_1"}
    assert events[-1].payload == {
        **events[-1].payload,
        "id": task_id,
        "stage_id": "stage_1",
        "owner": None,
    }


def test_deleting_a_world_drops_its_tasks(board: SqliteStorage) -> None:
    task_id = _add(board)
    keep = board.add_task("world_2", "run_2", "stage_9", "Other world", added_by=OPERATOR).id
    board.delete_world("world_1")
    assert board.get_task(task_id) is None
    assert board.get_task(keep) is not None
