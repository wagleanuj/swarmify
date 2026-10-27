"""The Scorecard: a read model over the world's event stream, no storage. Per run of a workflow:
first-try acceptance (`verdict.created`, loop 1), send-backs and cost (`run.updated`), duration
(the stages' times) and outcome (`outcome.recorded`, the published artifacts' views)."""

from collections import defaultdict
from datetime import datetime

from swarmify_kernel import Event
from swarmify_learning.model import RunScore


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def duration(stages: list[dict]) -> float | None:
    """Seconds from the first stage starting to the last ending; None before any started or
    while one still runs."""
    starts = [t for s in stages if (t := _when(s.get("started_at")))]
    if not starts:
        return None
    if any(s.get("status") in ("running", "waiting") for s in stages):
        return None
    ends = [t for s in stages if (t := _when(s.get("ended_at")))]
    return (max(ends) - min(starts)).total_seconds() if ends else None


def scorecard(events: list[Event], workflow_id: str) -> list[RunScore]:
    """Every run of the workflow, oldest first."""
    runs: dict[str, dict] = {}
    first: dict[str, dict[str, bool]] = defaultdict(dict)  # run -> stage -> first verdict passed
    views: dict[str, dict[str, int]] = defaultdict(dict)  # run -> artifact path -> latest views
    for e in events:
        p = e.payload
        if e.type == "run.updated" and p.get("workflow_id") == workflow_id:
            runs[p["id"]] = p
        elif e.type == "verdict.created" and p.get("loop") == 1:
            first[p["run_id"]][p["stage_id"]] = bool(p.get("passed"))
        elif e.type == "outcome.recorded" and e.run_id:
            views[e.run_id][p["path"]] = int(p.get("views") or 0)
    out = []
    for run_id, run in sorted(runs.items(), key=lambda kv: (kv[1]["created_at"], kv[0])):
        judged = first.get(run_id, {})
        measured = views.get(run_id)
        out.append(
            RunScore(
                run_id,
                run["name"],
                run.get("workflow_version"),
                bool(run.get("baseline")),
                run["status"],
                run["created_at"],
                len(judged),
                sum(judged.values()),
                int(run.get("send_backs") or 0),
                float(run.get("spent_usd") or 0.0),
                duration(run.get("stages") or []),
                sum(measured.values()) if measured else None,
            )
        )
    return out
