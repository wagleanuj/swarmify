"""The Workflows service over the SQLite adapter: versions are immutable and append-only."""

from contextlib import closing
from pathlib import Path

import pytest

from samples import WEEKLY_SHORT
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_workflows import Invalid, SqliteStorage, Workflows

WORLD = "world_1"
SMALL = "name: Trend sweep\nstages:\n  - gate: ok\n    ask: Fine?\n"


def test_first_save_creates_the_workflow_and_version_one(workflows: Workflows) -> None:
    version = workflows.save(WORLD, WEEKLY_SHORT)
    assert version.id.startswith("wfv_") and version.number == 1
    assert version.yaml == WEEKLY_SHORT and version.model.name == "Weekly short"
    assert [s.type for s in version.model.stages] == [
        "swarm",
        "gate",
        "swarm",
        "connector",
        "gate",
        "connector",
    ]
    [workflow] = workflows.list_all(WORLD)
    assert workflow.id == version.workflow_id and workflow.id.startswith("wf_")
    assert (workflow.name, workflow.world_id, workflow.current_version) == (
        "Weekly short",
        WORLD,
        1,
    )
    assert workflows.get(workflow.id) == workflow
    assert workflows.versions(workflow.id) == [version]
    assert workflows.version(workflow.id, 1) == version
    assert workflows.version(workflow.id, 2) is None
    assert workflows.list_all("world_other") == []


def test_each_save_appends_an_immutable_version_and_an_event(workflows: Workflows) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    v2 = workflows.save(WORLD, SMALL, v1.workflow_id)
    assert (v2.workflow_id, v2.number) == (v1.workflow_id, 2) and v2.id != v1.id
    assert [v.number for v in workflows.versions(v1.workflow_id)] == [1, 2]
    assert workflows.version(v1.workflow_id, 1) == v1, "the earlier version is untouched"
    workflow = workflows.get(v1.workflow_id)
    assert workflow and (workflow.name, workflow.current_version) == ("Trend sweep", 2)
    storage = workflows.storage
    assert isinstance(storage, SqliteStorage)
    with closing(connect(storage.path)) as conn:
        events = read_after(conn, WORLD, 0)
    assert [(e.type, e.payload["version"]) for e in events] == [
        ("workflow.saved", 1),
        ("workflow.saved", 2),
    ]
    assert events[1].payload == {"workflow_id": v1.workflow_id, "name": "Trend sweep", "version": 2}


def test_an_invalid_workflow_saves_nothing_and_names_every_violation(workflows: Workflows) -> None:
    bad = "name: Bad\nstages:\n  - connector: youtube\n    input: short.mp4\n  - swarm: s\n"
    with pytest.raises(Invalid) as exc:
        workflows.save(WORLD, bad)
    assert sorted((v.rule, v.stage) for v in exc.value.violations) == [
        ("input_not_produced", "youtube"),
        ("publish_without_gate", "youtube"),
        ("schema", "s"),
        ("schema", "s"),
    ]
    assert "must come right after a gate" in str(exc.value)
    assert workflows.list_all(WORLD) == []
    v1 = workflows.save(WORLD, SMALL)
    with pytest.raises(Invalid):
        workflows.save(WORLD, bad, v1.workflow_id)
    assert [v.number for v in workflows.versions(v1.workflow_id)] == [1]


def test_check_reports_without_saving(workflows: Workflows) -> None:
    draft = workflows.check(WORLD, "name: x\nstages:\n  - connector: nope\n")
    assert draft.model is not None and [v.rule for v in draft.violations] == ["unknown_connector"]
    assert workflows.check(WORLD, "- not: a workflow").model is None
    assert workflows.check(WORLD, WEEKLY_SHORT).violations == []
    assert workflows.list_all(WORLD) == []


def test_a_version_for_an_unknown_or_foreign_workflow_is_a_key_error(workflows: Workflows) -> None:
    with pytest.raises(KeyError):
        workflows.save(WORLD, SMALL, "wf_nope")
    v1 = workflows.save(WORLD, SMALL)
    with pytest.raises(KeyError):
        workflows.save("world_other", SMALL, v1.workflow_id)
    assert [v.number for v in workflows.versions(v1.workflow_id)] == [1]


def test_workflow_stages_resolve_against_stored_workflows_across_worlds(
    workflows: Workflows,
) -> None:
    child = workflows.save("world_other", SMALL)
    parent_yaml = f"name: Parent\nstages:\n  - workflow: sweep\n    runs: {child.workflow_id}\n"
    parent = workflows.save(WORLD, parent_yaml)
    # the child may not turn around and run its parent
    loop = f"name: Loop\nstages:\n  - workflow: up\n    runs: {parent.workflow_id}\n"
    with pytest.raises(Invalid) as exc:
        workflows.save("world_other", loop, child.workflow_id)
    assert [v.rule for v in exc.value.violations] == ["cycle"]
    # nor may a grandparent push the chain past two
    grand = f"name: Grand\nstages:\n  - workflow: p\n    runs: {parent.workflow_id}\n"
    workflows.save(WORLD, grand)
    great = f"name: Great\nstages:\n  - workflow: g\n    runs: {workflows.list_all(WORLD)[1].id}\n"
    with pytest.raises(Invalid) as exc:
        workflows.save(WORLD, great)
    assert [v.rule for v in exc.value.violations] == ["depth"]


def test_stored_yaml_round_trips_through_a_fresh_adapter(
    workflows: Workflows, tmp_path: Path
) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    again = Workflows(SqliteStorage(tmp_path / "swarmify.db"), lambda *_: {})
    assert again.versions(v1.workflow_id) == [v1]
