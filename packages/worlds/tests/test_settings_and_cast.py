"""World settings and the Cast: defaults, ranges, versions, and the record folded from events."""

from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel import Event
from swarmify_kernel.sqlite import connect, migrate
from swarmify_worlds import MIGRATIONS, Casting, Record, Skin, SqliteStorage, Worlds
from swarmify_worlds.cast import names, records


@pytest.fixture
def worlds(tmp_path: Path) -> Worlds:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return Worlds(SqliteStorage(db), tmp_path)


def test_a_new_world_has_default_settings_and_each_change_is_an_event(worlds: Worlds) -> None:
    world = worlds.create("Video")
    assert (world.retention_days, world.monthly_budget_usd, world.run_cap, world.agent_count) == (
        90,
        None,
        2,
        4,
    )
    changed = worlds.configure(world.id, {"monthly_budget_usd": 40.0, "run_cap": 1})
    assert changed is not None and (changed.monthly_budget_usd, changed.run_cap) == (40.0, 1)
    assert worlds.get(world.id) == changed
    event = worlds.events_after(world.id)[-1]
    assert (event.type, event.payload) == (
        "world.updated",
        {"id": world.id, "monthly_budget_usd": 40.0, "run_cap": 1},
    )
    cleared = worlds.configure(world.id, {"monthly_budget_usd": None, "retention_days": 0})
    assert cleared is not None and cleared.monthly_budget_usd is None
    assert cleared.retention_days == 0
    assert worlds.configure("world_nope", {"run_cap": 2}) is None
    assert worlds.events_after("world_nope") == []


@pytest.mark.parametrize(
    "changes",
    [
        {"monthly_budget_usd": 0},
        {"monthly_budget_usd": -1},
        {"run_cap": 0},
        {"run_cap": 9},
        {"retention_days": -1},
        {"retention_days": 3651},
        {"agent_count": 0},
        {"agent_count": 9},
        {"agent_count": 2.5},
        {"colour": "blue"},
        {"model": ""},
        {"model": "two words"},
        {"judge_model": 5},
        {"intern_model": "claude-haiku-4-5"},
        {"skin": None},
        {"skin": {"view": "map"}},
        {"skin": {"view": "office", "theme": "dark"}},
        {"skin": {"pack": "../up"}},
        {"skin": {"cast": {"judge": {"name": "x" * 25}}}},
        {"skin": {"cast": {"judge": {"colour": "red"}}}},
        {"skin": {"cast": {"intern": {"name": "Grave"}}}},
    ],
)
def test_settings_out_of_range_are_refused_and_nothing_is_written(
    worlds: Worlds, changes: dict
) -> None:
    world = worlds.create("Video")
    with pytest.raises(ValueError):
        worlds.configure(world.id, changes)
    assert worlds.get(world.id) == world
    assert [e.type for e in worlds.events_after(world.id)] == ["world.created"]


def test_the_skin_starts_plain_and_is_saved_whole_as_an_event(worlds: Worlds) -> None:
    world = worlds.create("Video")
    assert world.skin == Skin("plain")
    changed = worlds.configure(world.id, {"skin": {"view": "office"}})
    assert changed is not None and worlds.get(world.id) == changed
    assert changed.skin == Skin("office")
    assert worlds.events_after(world.id)[-1].payload == {"id": world.id, "skin": {"view": "office"}}
    look = {"character": "robot", "name": "Bolt", "colour": "#7fd6ff", "catchphrase": "Shipped."}
    dressed = worlds.configure(world.id, {"skin": {"pack": "mine", "cast": {"worker-1": look}}})
    assert dressed is not None and worlds.get(world.id) == dressed
    assert dressed.skin == Skin("plain", "mine", {"worker-1": Casting(**look)})


def test_shrinking_the_cast_drops_the_looks_of_agents_left_out(worlds: Worlds) -> None:
    world = worlds.create("Video")
    looks = {"worker-1": {"name": "Bolt"}, "worker-4": {"name": "Gone"}, "retro": {"name": "Grave"}}
    worlds.configure(world.id, {"skin": {"view": "office", "pack": "mine", "cast": looks}})
    shrunk = worlds.configure(world.id, {"agent_count": 1})
    assert shrunk is not None
    kept_looks = {"worker-1": Casting(name="Bolt"), "retro": Casting(name="Grave")}
    assert shrunk.skin == Skin("office", "mine", kept_looks)
    kept = {"view": "plain", "pack": "mine", "cast": {"worker-1": {"name": "Bolt"}}}
    switched = worlds.configure(world.id, {"skin": kept})
    assert switched is not None and switched.skin.view == "plain"


def test_the_model_for_a_role_is_its_override_then_the_world_default_then_none(
    worlds: Worlds,
) -> None:
    world = worlds.create("Video")
    assert (world.model, world.model_for("worker"), world.model_for(None)) == (None, None, None)
    changed = worlds.configure(
        world.id, {"model": "claude-sonnet-5-5", "judge_model": "claude-opus-5-5"}
    )
    assert changed is not None
    assert changed.model_for("judge") == "claude-opus-5-5"
    for role in ("planner", "worker", "retro", None):
        assert changed.model_for(role) == "claude-sonnet-5-5"
    cleared = worlds.configure(world.id, {"model": None, "retro_model": "claude-haiku-4-5"})
    assert cleared is not None
    assert (cleared.model_for("retro"), cleared.model_for("planner")) == ("claude-haiku-4-5", None)


def test_the_default_cast_is_a_planner_the_workers_and_a_judge(worlds: Worlds) -> None:
    world = worlds.create("Video")
    cast = worlds.cast(world.id)
    assert cast is not None
    assert (
        [(m.name, m.role) for m in cast]
        == names(4)
        == [
            ("planner", "planner"),
            ("worker-1", "worker"),
            ("worker-2", "worker"),
            ("worker-3", "worker"),
            ("worker-4", "worker"),
            ("judge", "judge"),
        ]
    )
    assert all((m.version, m.prompt, m.tools, m.record) == (1, "", None, Record()) for m in cast)
    worlds.configure(world.id, {"agent_count": 2})
    cast = worlds.cast(world.id)
    assert cast is not None
    assert [m.name for m in cast] == ["planner", "worker-1", "worker-2", "judge"]
    assert worlds.cast("world_nope") is None


def test_an_edit_appends_a_version_and_the_history_starts_at_the_default(worlds: Worlds) -> None:
    world = worlds.create("Video")
    tools = ["web_search", " graph"]
    edited = worlds.edit_cast(world.id, "worker-1", " Write hooks first. ", tools)
    assert edited is not None
    assert (edited.version, edited.prompt) == (2, "Write hooks first.")
    assert edited.tools == ["web_search", "graph"]
    event = worlds.events_after(world.id)[-1]
    assert event.type == "cast.updated" and event.payload["name"] == "worker-1"
    assert event.payload["version"] == 2

    again = worlds.edit_cast(world.id, "worker-1", "Write hooks first.", None)
    assert again is not None and again.version == 3 and again.tools is None
    cast = worlds.cast(world.id)
    assert cast is not None
    by_name = {m.name: m for m in cast}
    assert (by_name["worker-1"].version, by_name["worker-2"].version) == (3, 1)

    history = worlds.cast_versions(world.id, "worker-1")
    assert history is not None
    assert [(v.number, v.prompt, v.tools) for v in history] == [
        (1, "", None),
        (2, "Write hooks first.", ["web_search", "graph"]),
        (3, "Write hooks first.", None),
    ]
    with pytest.raises(ValueError, match="nothing changed"):
        worlds.edit_cast(world.id, "worker-1", "Write hooks first.", None)
    with pytest.raises(ValueError, match="at most"):
        worlds.edit_cast(world.id, "judge", "x" * 10_001, None)
    assert worlds.edit_cast(world.id, "worker-9", "Hi", None) is None
    assert worlds.cast_versions(world.id, "intern") is None
    assert worlds.edit_cast("world_nope", "judge", "Hi", None) is None

    assert worlds.delete(world.id)
    assert worlds.storage.cast_edits(world.id) == []


def _event(type: str, payload: dict, run_id: str = "run_1") -> Event:
    return Event.new(type, world_id="world_1", run_id=run_id, payload=payload)


def _task(task_id: str, owner: str, stage: str, status: str = "done") -> Event:
    payload = {"id": task_id, "stage_id": stage, "owner": owner, "status": status}
    return _event("task.updated", payload)


def _agent(agent_id: str, name: str, cost: float) -> Event:
    return _event("agent.updated", {"id": agent_id, "name": name, "cost_usd": cost})


def test_the_record_folds_tasks_first_verdicts_send_backs_and_cost(worlds: Worlds) -> None:
    run = {
        "stages": [{"id": "s1", "name": "research"}, {"id": "s2", "name": "script"}],
        "decisions": [
            {"id": "d1", "send_back_to": "script"},
            {"id": "d2", "send_back_to": None},
        ],
    }
    events = [
        _task("t1", "worker-1", "s1", "claimed"),
        _task("t1", "worker-1", "s1"),
        _task("t1", "worker-1", "s1"),  # a repeated snapshot counts once
        _task("t2", "worker-2", "s1"),
        _event("verdict.created", {"stage_id": "s1", "loop": 1, "passed": True}),
        _task("t3", "worker-1", "s2"),
        _event("verdict.created", {"stage_id": "s2", "loop": 1, "passed": False}),
        _event("verdict.created", {"stage_id": "s2", "loop": 2, "passed": True}),
        _event("run.updated", run),
        _event("run.updated", run),  # the same decision carried twice counts once
        _agent("a1", "worker-1", 0.5),
        _agent("a1", "worker-1", 0.75),  # snapshots carry the running total
        _agent("a2", "worker-1", 0.25),
        _agent("a3", "judge", 0.1),
        _task("t4", "worker-3", "s9"),  # a stage never judged
    ]
    folded = records(events)
    assert folded["worker-1"] == Record(
        tasks_done=2, judged=2, first_try=1, send_backs=1, cost_usd=1.0
    )
    assert folded["worker-2"] == Record(tasks_done=1, judged=1, first_try=1, send_backs=0)
    assert folded["worker-3"] == Record(tasks_done=1)
    assert folded["judge"] == Record(cost_usd=0.1)
    assert records([]) == {}
