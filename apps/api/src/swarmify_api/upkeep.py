"""Scheduled upkeep across contexts: the outcome write-back and file retention.

`record_outcomes` asks every publish connector a world enables that offers the `outcomes`
Capability for the Outcomes of all of the world's posts, whoever made them, and writes each
post's into its publish artifact (frontmatter and body, so the graph sees it). `prune` deletes
each world's run binaries older than its `retention_days` and keeps their markdown. Both are safe
to repeat: an unchanged Outcome is not written again, a deleted file is gone.
"""

import logging
from datetime import datetime, timedelta

from swarmify_connectors import ConnectorError, Connectors
from swarmify_memory import Memory
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)

UPKEEP_SECONDS = 6 * 3600.0
"""How often upkeep runs. Four reads a day stay far inside the platforms' quotas."""


def record_outcomes(worlds: Worlds, memory: Memory, registry: Connectors, now: datetime) -> int:
    """Write fresh Outcomes into publish artifacts; returns how many changed. A post's metrics
    from several connectors add up; its Primary Metric is its own connector's when that one
    answered, else the first that did. A connector that cannot answer (its Setup not done) is
    logged and skipped."""
    changed = 0
    for world in worlds.list_all():
        records = memory.publish_records(world.id)
        reporters = registry.outcome_reporters(world.id) if records else []
        posts = [
            {
                "connector": str(f.get("connector") or ""),
                "id": str(f.get("id") or path),
                "links": f.get("links") if isinstance(f.get("links"), dict) else {},
            }
            for path, f in records
        ]
        made_by = {p["id"]: p["connector"] for p in posts}
        found: dict[str, dict[str, float]] = {}
        primary: dict[str, str] = {}
        for connector in reporters:
            try:
                answer = registry.outcomes(connector, posts)
            except ConnectorError as exc:
                log.warning(
                    "outcomes from %s for world %s skipped: %s", connector.name, world.id, exc
                )
                continue
            for post, metrics in answer.items():
                if post not in made_by:
                    continue  # not one of the posts it was asked about
                mine = found.setdefault(post, {})
                for metric, n in metrics.items():
                    mine[metric] = mine.get(metric, 0) + n
                if post not in primary or made_by[post] == connector.name:
                    primary[post] = connector.manifest["primary_metric"]
        for (path, fields), post in zip(records, posts, strict=True):
            metrics = found.get(post["id"])
            if not metrics:
                continue
            ranked_by = primary[post["id"]]
            if (fields.get("outcomes"), fields.get("primary_metric")) != (metrics, ranked_by):
                memory.record_outcome(world.id, path, metrics, ranked_by, now)
                changed += 1
    return changed


def prune(worlds: Worlds, memory: Memory, now: datetime) -> list[str]:
    """Delete run binaries past each world's retention; a world set to 0 keeps them."""
    gone = []
    for world in worlds.list_all():
        if world.retention_days > 0:
            gone += memory.prune(world.id, now - timedelta(days=world.retention_days), now)
    return gone
