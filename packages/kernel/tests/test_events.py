from pathlib import Path

from swarmify_kernel import MIGRATIONS, Event
from swarmify_kernel.events import append, read_after
from swarmify_kernel.sqlite import connect, migrate


def test_event_envelope_gets_id_and_utc_timestamp() -> None:
    e = Event.new("world.created", world_id="world_1", payload={"name": "Video"})
    assert e.id.startswith("evt_")
    assert e.ts.endswith("+00:00")
    assert e.run_id is None and e.seq is None


def test_append_assigns_sequence_and_read_after_filters_by_world(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    migrate(conn, MIGRATIONS)
    with conn:
        a = append(conn, Event.new("world.created", world_id="w1", payload={"n": 1}))
        b = append(conn, Event.new("run.started", world_id="w1", run_id="r1", payload={}))
        append(conn, Event.new("world.created", world_id="w2", payload={}))
    assert (a.seq, b.seq) == (1, 2)
    events = read_after(conn, "w1", after_seq=0)
    assert [e.type for e in events] == ["world.created", "run.started"]
    assert events[0] == a and events[1].run_id == "r1"
    assert read_after(conn, "w1", after_seq=1) == [b]
    assert read_after(conn, "w1", after_seq=2) == []
