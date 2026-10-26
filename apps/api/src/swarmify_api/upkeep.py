"""Scheduled upkeep across contexts: the outcome write-back and file retention.

`record_outcomes` asks the `youtube` connector for the view count of every published video and
writes it into the publish artifact (frontmatter and body, so the graph sees it). `prune`
deletes each world's run binaries older than its `retention_days` and keeps their markdown.
Both are safe to repeat: an unchanged count is not written again, a deleted file is gone.
"""

import logging
from datetime import datetime, timedelta

from swarmify_connectors import ConnectorError, Connectors, publish
from swarmify_memory import Memory
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)

UPKEEP_SECONDS = 6 * 3600.0
"""How often upkeep runs. Four view-count reads a day stay far inside YouTube's quota."""


def record_outcomes(worlds: Worlds, memory: Memory, registry: Connectors, now: datetime) -> int:
    """Write fresh view counts into publish artifacts; returns how many changed. A world whose
    youtube connector cannot answer (no keys yet) is logged and skipped."""
    changed = 0
    for world in worlds.list_all():
        records = [
            (path, f, publish.youtube_ids(f)) for path, f in memory.publish_records(world.id)
        ]
        records = [r for r in records if r[2]]
        if not records:
            continue
        try:
            counts = registry.youtube_views(world.id, [i for *_, ids in records for i in ids])
        except ConnectorError as exc:
            log.warning("view counts for world %s skipped: %s", world.id, exc)
            continue
        for path, fields, ids in records:
            mine = {i: counts[i] for i in ids if i in counts}
            if mine and fields.get("views") != sum(mine.values()):
                memory.record_outcome(world.id, path, sum(mine.values()), mine, now)
                changed += 1
    return changed


def prune(worlds: Worlds, memory: Memory, now: datetime) -> list[str]:
    """Delete run binaries past each world's retention; a world set to 0 keeps them."""
    gone = []
    for world in worlds.list_all():
        if world.retention_days > 0:
            gone += memory.prune(world.id, now - timedelta(days=world.retention_days))
    return gone
