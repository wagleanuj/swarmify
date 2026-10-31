"""Shared sample data for the Workflows tests."""

from contextlib import closing
from pathlib import Path

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_workflows import MIGRATIONS, ConnectorRef


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return db


CONNECTORS = {
    c.name: c
    for c in [
        ConnectorRef("remotion_render", "stage", frozenset({"connector"})),
        ConnectorRef("youtube", "publish", frozenset({"connector"})),
    ]
}

WEEKLY_SHORT = """
name: Weekly short
schedule: "0 8 * * 1"
budget_usd: 5
agents: 4
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week.
    done_when: 5 ideas ranked by the judge
    tools: [web_search, graph]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea. Optional note for the script.
    kind: pick
  - swarm: script
    goal: Write script and shot list for the pick, 20-25s.
    done_when: script.md and shots.md written
    tools: [graph]
    produces: [script.md, shots.md]
  - connector: render
    calls: remotion_render
    input: script.md
    output: short.mp4
  - gate: review
    ask: Watch it. Approve or send back with a note.
  - connector: youtube
    input: short.mp4
    output: publish.md
    params: {visibility: scheduled}
"""
