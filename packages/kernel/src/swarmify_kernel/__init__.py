"""Shared kernel. Bounded contexts may import this and nothing else of each other."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def posts_privately(private_when: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    """Whether a publish stage's own `params` set every value its connector's `private_when`
    names, so the post stays private and needs no gate (ADR 0007 amended). A connector's default
    never counts. Shared here because Workflows checks it at save and Runs again at run time."""
    return bool(private_when) and all(params.get(k) == v for k, v in private_when.items())


__all__ = ["AGENT_TOOLS", "MIGRATIONS", "Event", "new_id", "posts_privately"]
