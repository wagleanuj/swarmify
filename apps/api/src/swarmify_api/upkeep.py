"""Scheduled upkeep across contexts: the outcome write-back, file retention, and the notices the
Needs-you page shows.

`record_outcomes` asks every publish connector a world enables that offers the `outcomes`
Capability for the Outcomes of all of the world's posts, whoever made them, and records each
post's beside its publish artifact (Memory.record_outcome), never in it, so the graph does not
re-extract the world on every view count. `prune` deletes
each world's run binaries older than its `retention_days` and keeps their markdown. Both are safe
to repeat: an unchanged Outcome is not written again, a deleted file is gone. `notices_router`
lists what the operator should fix that no gate asks about.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from swarmify_connectors import VISIBILITY_LABEL, VISIBLE, ConnectorError, Connectors, setup
from swarmify_memory import Memory
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)

UPKEEP_SECONDS = 6 * 3600.0
"""How often upkeep runs. Four reads a day stay far inside the platforms' quotas."""
HIDDEN_AFTER = timedelta(hours=24)
"""How long a post may stay hidden from everyone before the Needs-you page says so."""


class Notice(BaseModel):
    """Something the operator should fix that no gate asks about."""

    kind: Literal["sign_in", "outcomes", "visibility"]
    text: str
    world_id: str | None = None
    """The world whose Connectors page (sign_in, outcomes) or run (visibility) fixes it."""
    run_id: str | None = None


def record_outcomes(
    worlds: Worlds,
    memory: Memory,
    registry: Connectors,
    now: datetime,
    notices: dict[str, str] | None = None,
) -> int:
    """Record fresh Outcomes of publish artifacts; returns how many changed. A post's metrics
    from several connectors add up; its Primary Metric is its own connector's when that one
    answered, else the first that did; whether everyone can see it (`visible`, with the
    platform's `visibility_label`) is what the first connector to say reported. A
    connector that cannot answer (its Setup not done) is logged and skipped. What a connector
    says it could not read goes in `notices` by connector name, logged once, gone once it can."""
    changed = 0
    noticed: dict[str, str] = {}
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
        seen: dict[str, dict[str, float | bool | str]] = {}
        primary: dict[str, str] = {}
        for connector in reporters:
            try:
                answer, notice = registry.outcomes(connector, posts)
            except ConnectorError as exc:
                log.warning(
                    "outcomes from %s for world %s skipped: %s", connector.name, world.id, exc
                )
                continue
            if notice:
                noticed[connector.name] = notice
            for post, metrics in answer.items():
                if post not in made_by:
                    continue  # not one of the posts it was asked about
                mine = found.setdefault(post, {})
                for metric, n in metrics.items():
                    if metric not in (VISIBLE, VISIBILITY_LABEL) and not isinstance(n, bool | str):
                        mine[metric] = mine.get(metric, 0) + n
                if isinstance(metrics.get(VISIBLE), bool) and post not in seen:
                    seen[post] = {
                        k: metrics[k] for k in (VISIBLE, VISIBILITY_LABEL) if k in metrics
                    }
                if post not in primary or made_by[post] == connector.name:
                    primary[post] = connector.manifest["primary_metric"]
        for (path, fields), post in zip(records, posts, strict=True):
            metrics = found.get(post["id"])
            if not metrics:
                continue
            ranked_by, shown = primary[post["id"]], seen.get(post["id"], {})
            was = {k: fields[k] for k in (VISIBLE, VISIBILITY_LABEL) if k in fields}
            if (fields.get("outcomes"), fields.get("primary_metric"), was) != (
                metrics,
                ranked_by,
                shown,
            ):
                memory.record_outcome(world.id, path, metrics, ranked_by, now, shown)
                changed += 1
    if notices is not None:
        for name, text in noticed.items():
            if notices.get(name) != text:
                log.warning("outcomes from %s: %s", name, text)
        notices.clear()
        notices.update(noticed)
    return changed


def _when(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


def hidden_posts(
    records: Iterable[tuple[str, Mapping[str, Any]]],
    run_status: Callable[[str], str | None],
    now: datetime,
    labels: Mapping[str, str],
) -> list[tuple[str, str]]:
    """(run id, why) of each post its connector still reports hidden from everyone (`visible`
    false) a day after it went out, when its connector said it went out `public`, or its stage
    never said who sees it (`visibility_said` false) and its run is done. A post scheduled for
    later is not late yet. The words are the connector's: its label by name in `labels`, and
    its `visibility_label` for the post."""
    out = []
    for _, f in records:
        visible, label = f.get(VISIBLE), f.get(VISIBILITY_LABEL)
        public, said, later = f.get("public"), f.get("visibility_said"), f.get("scheduled_for")
        # Legacy, records from youtube 1.2.0 (#446) and before: the platform's `privacy` (the
        # next upkeep replaces it), the stage's own `visibility` and the `schedule` it was sent.
        if visible is None and isinstance(f.get("privacy"), str):
            visible, label = f["privacy"] == "public", f["privacy"]
        if said is None:
            said, public = "visibility" in f, f.get("visibility") == "public"
            sent = {
                x.get("key"): x.get("value") for x in f.get("sent") or [] if isinstance(x, dict)
            }
            later = sent.get("schedule")
        run, posted = str(f.get("run") or ""), _when(f.get("created"))
        if visible is not False or posted is None or now - posted < HIDDEN_AFTER:
            continue
        if (when := _when(later)) is not None and when > now:
            continue
        name = labels.get(str(f.get("connector"))) or f.get("connector")
        if public is True:
            why = "though it went out for everyone to see"
        elif said is False and run_status(run) == "done":
            why = f"and its stage never said who sees it, so {name}'s default chose"
        else:
            continue
        title = f.get("title") or "A post"
        out.append((run, f"{title} is still {label or 'not public'} on {name} a day on, {why}."))
    return out


def notices_router(
    worlds: Worlds,
    memory: Memory,
    registry: Connectors,
    run_status: Callable[[str], str | None],
    outcome_notices: Mapping[str, str],
    clock: Callable[[], datetime],
) -> APIRouter:
    """What the Needs-you page lists beside the gates: an account whose sign-in the provider
    refused (an expired refresh token, say), what a connector could not read on the last
    upkeep, and a post still hidden a day after it went out."""
    api = APIRouter(prefix="/api", tags=["notices"])

    @api.get("/notices")
    def notices() -> list[Notice]:
        out: list[Notice] = []
        signed_out: set[tuple[str, str | None]] = set()
        everyone = worlds.list_all()
        for world in everyone:
            connectors = registry.list_all(world.id)
            labels = {c.name: c.manifest.get("label") or c.name for c in connectors}
            for c in connectors:
                keys = registry.sign_in_needed(c)
                if not keys:
                    continue
                fields = [f for f in setup.fields(c.manifest) if f["key"] in keys]
                # a shared account is one sign-in for every world: one notice
                whose = (
                    c.name,
                    None if all(setup.scope(f) != "world" for f in fields) else world.id,
                )
                if whose in signed_out:
                    continue
                signed_out.add(whose)
                label = labels[c.name]
                text = (
                    f"{label} needs sign-in: the provider refused its saved sign-in (expired or"
                    " revoked), so it can neither post nor read outcomes. Sign in again on the"
                    " Connectors page."
                )
                out.append(Notice(kind="sign_in", text=text, world_id=world.id))
            records = memory.publish_records(world.id)
            for run, text in hidden_posts(records, run_status, clock(), labels):
                out.append(Notice(kind="visibility", text=text, world_id=world.id, run_id=run))
        first = everyone[0].id if everyone else None
        out += [Notice(kind="outcomes", text=t, world_id=first) for t in outcome_notices.values()]
        return out

    return api


def prune(worlds: Worlds, memory: Memory, now: datetime) -> list[str]:
    """Delete run binaries past each world's retention; a world set to 0 keeps them."""
    gone = []
    for world in worlds.list_all():
        if world.retention_days > 0:
            gone += memory.prune(world.id, now - timedelta(days=world.retention_days), now)
    return gone
