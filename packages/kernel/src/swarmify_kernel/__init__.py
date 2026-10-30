"""Shared kernel. Bounded contexts may import this and nothing else of each other."""

from pathlib import Path

from swarmify_kernel.events import Event
from swarmify_kernel.ids import new_id

MIGRATIONS = Path(__file__).parent / "migrations"

AGENT_TOOLS = {
    "web_search": "the web, through Claude's own WebSearch and WebFetch",
    "graph": "this world's graph, read only",
}
"""The Agent Tools (ADR 0023): names a swarm stage's `tools` may list that the core itself
provides, not a Connector, so they have no package, version or Credentials. Shared here because
Workflows checks the names, Swarm grants them and Connectors keeps them free."""

__all__ = ["AGENT_TOOLS", "MIGRATIONS", "Event", "new_id"]
