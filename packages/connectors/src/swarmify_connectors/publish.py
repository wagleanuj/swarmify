"""Publish stages: what will go out, the call that sends it, and what came back (ADR 0007).

Every publish connector stage declares the same params: `title`, `description`, `visibility`
(public, unlisted or private) and an optional `schedule` (ISO 8601 with a timezone). The confirm
gate before it shows `preview`, and `request` sends exactly those values, so what goes out is
what the person saw. A scheduled video stays private until its time, then goes public, so a
schedule needs `visibility: public`.

`youtube` is our wrapper (`swarmify_connectors.youtube`); `upload_post` is Upload-Post's official
MCP, adopted as is (ADR 0015), whose `upload_video` takes its own argument names and also needs
`user` (the Upload-Post profile) and `platforms`.
"""

from datetime import UTC, datetime
from typing import Any

VISIBILITIES = ("public", "unlisted", "private")
TITLE_MAX = 100  # YouTube's limit, the tightest of the platforms we post to


def _params(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("params") or {})


def _schedule(value: Any) -> tuple[str | None, str | None]:
    """(UTC ISO time, problem)."""
    if value in (None, ""):
        return None, None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None, f"schedule {value!r} is not an ISO 8601 time like 2026-11-03T09:00:00Z"
    if when.tzinfo is None:
        return None, f"schedule {value!r} needs a timezone, like 2026-11-03T09:00:00Z"
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


def preview(connector: str, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the stage will publish, plus every problem that would stop it."""
    params = _params(config)
    title = str(params.get("title") or "").strip()
    visibility = str(params.get("visibility") or "private")
    schedule, bad_schedule = _schedule(params.get("schedule"))
    problems = [bad_schedule] if bad_schedule else []
    if not title:
        problems.append("a publish stage needs params.title")
    elif len(title) > TITLE_MAX:
        problems.append(f"the title is {len(title)} characters, the limit is {TITLE_MAX}")
    if visibility not in VISIBILITIES:
        problems.append(f"visibility is one of {', '.join(VISIBILITIES)}, got {visibility!r}")
    if schedule and visibility != "public":
        problems.append("a scheduled video goes public at its time; set visibility: public")
    if schedule and schedule <= (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"):
        problems.append(f"the schedule {schedule} has already passed")
    if not config.get("input"):
        problems.append("a publish stage needs an input: the video to post")
    platforms: list[str] = []
    if connector == "upload_post":
        raw = params.get("platforms") or ["youtube"]
        platforms = [str(p) for p in (raw if isinstance(raw, list) else [raw])]
        if not str(params.get("user") or "").strip():
            problems.append("upload_post needs params.user, the Upload-Post profile to post as")
    return {
        "connector": connector,
        "title": title,
        "description": str(params.get("description") or ""),
        "visibility": visibility,
        "schedule": schedule,
        "video": config.get("input"),
        "platforms": platforms,
        "problems": problems,
    }


def request(
    connector: str, config: dict[str, Any], idempotency_key: str
) -> tuple[str, dict[str, Any]] | None:
    """The tool and arguments for a built-in publish connector; None for one a world added,
    which gets the generic connector call. `params.input_path` is set by the run."""
    params = _params(config)
    shown = preview(connector, config)
    video = params.get("input_path") or ""
    if connector == "youtube":
        return "youtube", {
            "idempotency_key": idempotency_key,
            "input_path": video,
            "title": shown["title"],
            "description": shown["description"],
            "visibility": shown["visibility"],
            "schedule": shown["schedule"] or "",
            "made_for_kids": bool(params.get("made_for_kids", False)),
        }
    if connector == "upload_post":
        args: dict[str, Any] = {
            "videoPathOrUrl": video,
            "user": str(params["user"]).strip(),
            "platforms": shown["platforms"],
            "title": shown["title"],
            "description": shown["description"],
            "asyncUpload": False,  # answer with the post links, not a request id to poll
            # platformOptions passes through to the SDK, which sends it as Idempotency-Key.
            "platformOptions": {
                "idempotencyKey": idempotency_key,
                "youtubePrivacyStatus": shown["visibility"],
            },
        }
        if shown["schedule"]:
            args["scheduledDate"] = shown["schedule"]
        return "upload_video", args
    return None


def result(connector: str, response: dict[str, Any]) -> dict[str, Any]:
    """What came back, normalized: `status` (published or scheduled), `links` (platform to URL),
    `id` (the video or scheduler job) and `failed` (platform to error)."""
    if connector == "upload_post":
        body = response.get("result", response)
        body = body if isinstance(body, dict) else {}
        if body.get("job_id"):
            return {"status": "scheduled", "links": {}, "id": str(body["job_id"]), "failed": {}}
        raw = body.get("results")
        results: dict[str, Any] = raw if isinstance(raw, dict) else {}
        links = {p: r["url"] for p, r in results.items() if r.get("success") and r.get("url")}
        failed = {
            p: str(r.get("error") or "failed") for p, r in results.items() if not r.get("success")
        }
        return {"status": "published", "links": links, "id": None, "failed": failed}
    url = response.get("url")
    return {
        "status": "scheduled" if response.get("publish_at") else "published",
        "links": {connector: url} if url else {},
        "id": response.get("video_id") or response.get("id"),
        "failed": {},
    }


def markdown(shown: dict[str, Any], out: dict[str, Any]) -> str:
    """The publish artifact body. The link is repeated in the body because the graph reads
    bodies, not frontmatter."""
    if out["links"]:
        lines = [f"- {p}: {url}" for p, url in out["links"].items()]
    else:
        job = f"{shown['connector']} job {out['id']}"
        lines = [f"- no link yet: {job} posts at {shown['schedule']}"]
    lines += [f"- {p} failed: {error}" for p, error in out["failed"].items()]
    how = (
        f"private until {shown['schedule']}, then {shown['visibility']}"
        if shown["schedule"]
        else f"{shown['visibility']}, now"
    )
    return (
        f"# {shown['title']}\n\n"
        f"{shown['video']} went to {shown['connector']}: {how}.\n\n" + "\n".join(lines) + "\n"
    )
