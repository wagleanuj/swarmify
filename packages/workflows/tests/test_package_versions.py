"""Each saved workflow version records the Connector Package version of every package it calls
(issue 223); the stamp fills in what an older version lacks, and never changes a record."""

from pathlib import Path

from samples import CONNECTORS, migrated_db
from swarmify_workflows import ConnectorRef, SqliteStorage, Workflows

YAML = """
name: Echo
stages:
  - swarm: research
    goal: Find one idea.
    done_when: one idea
    tools: [web_search, lookup]
    produces: [idea.md]
  - connector: echo
    input: idea.md
  - connector: render
    calls: remotion_render
    input: idea.md
    output: short.mp4
"""


def test_a_save_records_the_pinned_version_of_each_package_it_calls(tmp_path: Path) -> None:
    pins = {"echo": "1.0.0", "lookup": "0.2.0", "unused": "3.0.0"}

    def refs(_world_id: str) -> dict[str, ConnectorRef]:
        stage = frozenset({"swarm", "connector"})
        packaged = {n: ConnectorRef(n, "stage", stage, version=v) for n, v in pins.items()}
        return {**CONNECTORS, **packaged}

    workflows = Workflows(SqliteStorage(migrated_db(tmp_path)), refs)
    first = workflows.save("w1", YAML)
    assert first.packages == {"echo": "1.0.0", "lookup": "0.2.0"}

    pins["echo"] = "2.0.0"  # the world moves its pin: v1 keeps its record, v2 records the new
    second = workflows.save("w1", YAML, first.workflow_id)
    assert workflows.version(first.workflow_id, 1).packages == {"echo": "1.0.0", "lookup": "0.2.0"}  # type: ignore[union-attr]
    assert second.packages == {"echo": "2.0.0", "lookup": "0.2.0"}
    assert [(w.id, n) for w, n in workflows.using("echo", "1.0.0")] == [(first.workflow_id, 1)]
    assert workflows.using("echo", "9.9.9") == []


def test_the_stamp_fills_in_only_what_a_version_calls_and_lacks(tmp_path: Path) -> None:
    storage = SqliteStorage(migrated_db(tmp_path))
    old = Workflows(storage, lambda _: CONNECTORS).save("w1", WEEKLY)  # saved before versions
    assert old.packages == {}

    workflows = Workflows(storage, lambda _: CONNECTORS)
    defaults = {"youtube": "1.0.0", "remotion_render": "1.0.0", "speech": "1.0.0"}
    assert workflows.stamp(lambda world_id: defaults if world_id == "w1" else {}) == 1
    stamped = workflows.version(old.workflow_id, 1)
    assert stamped is not None
    assert stamped.packages == {"youtube": "1.0.0", "remotion_render": "1.0.0"}

    later = {"youtube": "2.0.0", "remotion_render": "2.0.0"}
    assert workflows.stamp(lambda _: later) == 0  # a recorded version never moves
    assert workflows.version(old.workflow_id, 1).packages == stamped.packages  # type: ignore[union-attr]


WEEKLY = """
name: Weekly
stages:
  - swarm: script
    goal: Write the script.
    done_when: script.md written
    produces: [script.md]
  - connector: render
    calls: remotion_render
    input: script.md
    output: short.mp4
  - gate: review
    ask: Approve?
  - connector: youtube
    input: short.mp4
    output: publish.md
"""
