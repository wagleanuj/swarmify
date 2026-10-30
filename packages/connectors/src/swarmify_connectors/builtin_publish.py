"""The built-in publish connectors' own `preview` and `publish` (ADR 0019): the video fields they
send and how they map them to their tools. The core never reads these fields; it shows what
`preview` answers and sends what was confirmed (`swarmify_connectors.publish`). Each half moves
into its connector's package with issue 111.

Both take the same params: `title`, `description`, `visibility` (public, unlisted or private),
`made_for_kids` (YouTube's COPPA flag, false by default) and an optional `schedule`: ISO 8601
with a timezone, or relative, `HH:MM` (the next such time) or `+Nd HH:MM` (N days from now),
either with an optional IANA timezone after it (UTC by default), resolved against `now`. The
metadata file's `title`, `description`, `hashtags` and `tags` win over the params, and the gate's
edits over all. `hashtags` (a list, or one string; `#` optional) go out as the description's last
line, where YouTube shows up to three above the title; `tags` (a list, or one comma-separated
string) go to YouTube's tags beside the retry tag.
A scheduled video stays private until its time, then goes public, so a schedule needs
`visibility: public`.

`youtube` is our wrapper (`swarmify_connectors.youtube`); `upload_post` is Upload-Post's official
MCP, adopted as is (ADR 0015), whose `upload_video` takes its own argument names and also needs
`user` (the Upload-Post profile) and `platforms`.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NAMES = ("youtube", "upload_post")
VISIBILITIES = ("public", "unlisted", "private")
UNFINISHED = ("pending", "in_progress", "processing")  # get_status while it works
TITLE_MAX = 100  # YouTube's limit, the tightest of the platforms we post to
DESCRIPTION_BYTES = 5_000  # YouTube's limit
HASHTAGS_MAX = 5  # well under YouTube's 60: more reads as spam (research 2026-10-30)
TAGS_CHARS = 500  # YouTube's limit for all tags together, commas and quotes included
RETRY_TAG = "swarmify-"  # youtube.py tags each upload swarmify-<idempotency key>
RETRY_ROOM = 50  # what that tag and its comma can take of TAGS_CHARS
EDITABLE = ("title", "description", "hashtags", "tags")
_HASHTAG = re.compile(r"^\w+$")


def _words(value: Any, split: str) -> list[str]:
    """A list, or one string split on `split`, without empties."""
    raw = value if isinstance(value, list) else re.split(split, str(value or ""))
    return [w for w in (str(v).strip() for v in raw) if w]


def hashtags(value: Any) -> list[str]:
    """Each hashtag without its `#`."""
    return [w.lstrip("#") for w in _words(value, r"[\s,]+") if w.lstrip("#")]


def tags(value: Any) -> list[str]:
    return _words(value, r",")


def tags_length(names: list[str]) -> int:
    """How YouTube counts tags: a tag with a space is counted as if quoted, commas count."""
    return sum(len(t) + (2 if " " in t else 0) for t in names) + max(len(names) - 1, 0)


def description(text: str, tagged: list[str]) -> str:
    """The description as it goes out: the hashtags as its last line."""
    line = " ".join(f"#{h}" for h in tagged)
    return f"{text.rstrip()}\n\n{line}" if text.strip() and line else text or line


_RELATIVE = re.compile(r"^(?:\+(\d+)d\s+)?([01]?\d|2[0-3]):([0-5]\d)(?:\s+(\S+))?$")


def _schedule(value: Any, now: datetime) -> tuple[str | None, str | None]:
    """(UTC ISO time, problem)."""
    if value in (None, ""):
        return None, None
    if relative := _RELATIVE.match(str(value).strip()):
        days, hour, minute, zone = relative.groups()
        try:
            local = now.astimezone(ZoneInfo(zone) if zone else UTC)
        except (ZoneInfoNotFoundError, ValueError):
            return None, f"schedule {value!r} names an unknown timezone {zone!r}"
        when = (local + timedelta(days=int(days or 0))).replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
        if days is None and when <= local:
            when += timedelta(days=1)
        return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None, (
            f"schedule {value!r} is not an ISO 8601 time like 2026-11-03T09:00:00Z"
            " or a time of day like 09:00 or +1d 09:00"
        )
    if when.tzinfo is None:
        return None, f"schedule {value!r} needs a timezone, like 2026-11-03T09:00:00Z"
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


def _field(key: str, label: str, value: Any, hint: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "editable": key in EDITABLE, "hint": hint}


def preview(connector: str, args: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the stage will publish, plus every problem that would stop it. `args` is the core's
    preview call: `inputs` (file names), `params`, `metadata` (frontmatter) and `edits`."""
    metadata = args.get("metadata") or {}
    edits = {k: v for k, v in (args.get("edits") or {}).items() if k in EDITABLE}
    params = {
        **(args.get("params") or {}),
        **{k: metadata[k] for k in EDITABLE if metadata.get(k)},
        **edits,
    }
    now = now or datetime.now(UTC)
    title = str(params.get("title") or "").strip()
    visibility = str(params.get("visibility") or "private")
    made_for_kids = params.get("made_for_kids", False)
    schedule, bad_schedule = _schedule(params.get("schedule"), now)
    problems = [bad_schedule] if bad_schedule else []
    if not isinstance(made_for_kids, bool):
        problems.append(f"made_for_kids is true or false, got {made_for_kids!r}")
    # The workflow check passes `metadata` as configured, so a title may still come from that
    # file; at the gate the core has read it and passes its frontmatter instead.
    if not title and not params.get("metadata"):
        problems.append("a publish stage needs params.title")
    elif len(title) > TITLE_MAX:
        problems.append(f"the title is {len(title)} characters, the limit is {TITLE_MAX}")
    if visibility not in VISIBILITIES:
        problems.append(f"visibility is one of {', '.join(VISIBILITIES)}, got {visibility!r}")
    if schedule and visibility != "public":
        problems.append("a scheduled video goes public at its time; set visibility: public")
    if schedule and schedule <= now.strftime("%Y-%m-%dT%H:%M:%SZ"):
        problems.append(f"the schedule {schedule} has already passed")
    tagged, keywords = hashtags(params.get("hashtags")), tags(params.get("tags"))
    bad = [h for h in tagged if not _HASHTAG.match(h)]
    if bad:
        problems.append(
            f"a hashtag is one word of letters, digits or _, not {', '.join('#' + b for b in bad)}"
        )
    if len(tagged) > HASHTAGS_MAX:
        problems.append(f"{len(tagged)} hashtags, at most {HASHTAGS_MAX}")
    if tags_length(keywords) > TAGS_CHARS - RETRY_ROOM:
        problems.append(
            f"the tags take {tags_length(keywords)} characters, at most"
            f" {TAGS_CHARS - RETRY_ROOM} with room for the retry tag"
        )
    text = str(params.get("description") or "").strip()
    size = len(description(text, tagged).encode())
    if size > DESCRIPTION_BYTES:
        problems.append(
            f"the description with its hashtags is {size} bytes, the limit is {DESCRIPTION_BYTES}"
        )
    inputs = args.get("inputs") or []
    if not inputs:
        problems.append("a publish stage needs an input: the video to post")
    fields = [
        _field("title", "Title", title),
        _field("description", "Description", text),
        _field("hashtags", "Hashtags", " ".join(f"#{h}" for h in tagged), "The last line."),
        _field("tags", "Tags", ", ".join(keywords)),
    ]
    if connector == "upload_post":
        raw = params.get("platforms") or ["youtube"]
        platforms = [str(p) for p in (raw if isinstance(raw, list) else [raw])]
        fields.append(_field("platforms", "Platforms", platforms))
        if not str(params.get("user") or "").strip():
            problems.append("upload_post needs params.user, the Upload-Post profile to post as")
    if schedule:
        when = (
            f"Private until then, then {visibility}."
            if connector == "youtube"
            else "Nothing posts before then."
        )
    else:
        when = "Right after confirming."
    fields += [
        _field("visibility", "Visibility", visibility),
        _field("made_for_kids", "Made for kids", made_for_kids is True),
        _field("schedule", "When", schedule, when),
        _field("video", "Video", inputs[0] if inputs else None),
    ]
    return {"fields": fields, "problems": problems}


def request(
    connector: str, values: dict[str, Any], params: dict[str, Any], idempotency_key: str
) -> tuple[str, dict[str, Any]]:
    """The tool and arguments that post the confirmed `values`. `params.input_path` is set by the
    run; `params.user` is Upload-Post's profile."""
    video = params.get("input_path") or ""
    text = description(str(values["description"]), hashtags(values.get("hashtags")))
    keywords = tags(values.get("tags"))
    if connector == "youtube":
        return "youtube", {
            "idempotency_key": idempotency_key,
            "input_path": video,
            "title": values["title"],
            "description": text,
            "tags": keywords,
            "visibility": values["visibility"],
            "schedule": values["schedule"] or "",
            "made_for_kids": values["made_for_kids"],
        }
    args: dict[str, Any] = {
        "videoPathOrUrl": video,
        "user": str(params["user"]).strip(),
        "platforms": values["platforms"],
        "title": values["title"],
        "description": text,
        "asyncUpload": False,  # answer with the post links, not a request id to poll
        # platformOptions passes through to the SDK, which sends it as Idempotency-Key.
        "platformOptions": {
            "idempotencyKey": idempotency_key,
            "youtubePrivacyStatus": values["visibility"],
            "youtubeSelfDeclaredMadeForKids": values["made_for_kids"],
            **({"youtubeTags": keywords} if keywords else {}),
        },
    }
    if values["schedule"]:
        args["scheduledDate"] = values["schedule"]
    return "upload_video", args


def result(connector: str, response: dict[str, Any]) -> dict[str, Any]:
    """What came back, normalized (`swarmify_connectors.publish.result`). Upload-Post moves an
    upload over 59 seconds to the background even when asked to wait, answering a `request_id`
    and no results: that is `pending` until its `get_status` answer is no longer pending or in
    progress. That answer lists `results` per platform, with `post_url` and `message` where the
    upload answer has `url` and `error`. An answer with no results that is `success: false` (an
    unknown or expired request) or a failed status is a failure, never an empty success."""
    if connector == "upload_post":
        body = response.get("result", response)
        body = body if isinstance(body, dict) else {}
        if body.get("job_id"):
            return {"status": "scheduled", "links": {}, "id": str(body["job_id"]), "failed": {}}
        raw = body.get("results")
        status = body.get("status")
        if status in UNFINISHED or body.get("request_id") and not raw and not status:
            pending = str(body.get("request_id") or "") or None
            return {"status": "pending", "links": {}, "id": pending, "failed": {}}
        if isinstance(raw, list):
            raw = {str(r.get("platform")): r for r in raw if isinstance(r, dict)}
        results: dict[str, Any] = raw if isinstance(raw, dict) else {}
        links = {
            p: r.get("url") or r["post_url"]
            for p, r in results.items()
            if r.get("success") and (r.get("url") or r.get("post_url"))
        }
        failed = {
            p: str(r.get("error") or r.get("message") or "failed")
            for p, r in results.items()
            if not r.get("success")
        }
        if not results and (status in ("failed", "error") or body.get("success") is False):
            failed = {"upload_post": str(body.get("message") or body.get("error") or status)}
        return {"status": "published", "links": links, "id": None, "failed": failed}
    url = response.get("url")
    return {
        "status": "scheduled" if response.get("publish_at") else "published",
        "links": {connector: url} if url else {},
        "id": response.get("video_id") or response.get("id"),
        "failed": {},
    }
