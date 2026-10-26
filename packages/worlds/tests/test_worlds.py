from pathlib import Path

import pytest

from swarmify_worlds import SqliteStorage, Worlds


@pytest.fixture
def worlds(tmp_path: Path) -> Worlds:
    return Worlds(SqliteStorage(tmp_path / "swarmify.db"), tmp_path)


def test_create_gives_the_world_a_folder_a_row_and_a_created_event(worlds: Worlds) -> None:
    world = worlds.create("  Video ", "Weekly shorts for the attraction channel.")
    assert world.id.startswith("world_")
    assert (world.name, world.purpose) == ("Video", "Weekly shorts for the attraction channel.")
    assert worlds.folder(world.id).is_dir()
    assert worlds.get(world.id) == world
    assert worlds.list_all() == [world]
    [event] = worlds.events_after(world.id)
    assert event.type == "world.created"
    assert event.payload == {"id": world.id, "name": "Video", "purpose": world.purpose}
    assert event.seq == 1


def test_create_rejects_a_blank_name(worlds: Worlds) -> None:
    with pytest.raises(ValueError, match="name"):
        worlds.create("   ")
    assert worlds.list_all() == []


def test_list_is_in_creation_order(worlds: Worlds) -> None:
    a = worlds.create("A")
    b = worlds.create("B")
    assert [w.id for w in worlds.list_all()] == [a.id, b.id]


def test_delete_removes_folder_and_row_and_records_the_event(worlds: Worlds) -> None:
    world = worlds.create("Video")
    (worlds.folder(world.id) / "runs").mkdir()
    assert worlds.delete(world.id) is True
    assert not worlds.folder(world.id).exists()
    assert worlds.get(world.id) is None
    assert worlds.list_all() == []
    assert [e.type for e in worlds.events_after(world.id)] == ["world.created", "world.deleted"]


def test_delete_of_unknown_world_is_false_and_records_nothing(worlds: Worlds) -> None:
    assert worlds.delete("world_nope") is False
    assert worlds.events_after("world_nope") == []


def test_storage_reopens_an_existing_database_without_reapplying_migrations(tmp_path: Path) -> None:
    first = Worlds(SqliteStorage(tmp_path / "swarmify.db"), tmp_path)
    world = first.create("Video")
    second = Worlds(SqliteStorage(tmp_path / "swarmify.db"), tmp_path)
    assert second.list_all() == [world]
