"""The built-in youtube connector's own `preview` and `publish` (ADR 0019): the video fields it
sends and how it maps them to its tool. The core never reads these fields; it shows what
`preview` answers and sends what was confirmed (`swarmify_connectors.publish`). It moves into its
package with issue 214, as upload_post's did (connectors/upload_post).

It takes these params: `title`, `description`, `visibility` (public, unlisted or private),
`made_for_kids` (YouTube's COPPA flag, false by default) and an optional `schedule`: ISO 8601
with a timezone, or relative, `HH:MM` (the next such time) or `+Nd HH:MM` (N days from now),
either with an optional IANA timezone after it (UTC by default), resolved against `now`. The
metadata file's `title`, `description`, `hashtags` and `tags` win over the params, and the gate's
edits over all. `hashtags` (a list, or one string; `#` optional) go out as the description's last
line, where YouTube shows up to three above the title; `tags` (a list, or one comma-separated
string) go to YouTube's tags beside the retry tag.
A scheduled video stays private until its time, then goes public, so a schedule needs
`visibility: public`.

`youtube` is our wrapper (`swarmify_connectors.youtube`).
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NAMES = ("youtube",)
VISIBILITIES = ("public", "unlisted", "private")
TITLE_MAX = 100  # YouTube's limit, the tightest of the platforms we post to
DESCRIPTION_BYTES = 5_000  # YouTube's limit
HASHTAGS_MAX = 5  # well under YouTube's 60: more reads as spam (research 2026-10-30)
TAGS_CHARS = 500  # YouTube's limit for all tags together, commas and quotes included
# youtube.py tags each upload swarmify-<stage id>.<visit>: about 45 characters with its comma.
TAGS_ROOM = TAGS_CHARS - 50
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
    line = hashtag_line(tagged)
    return f"{text.strip()}\n\n{line}" if text.strip() and line else text.strip() or line


def hashtag_line(names: list[str]) -> str:
    return " ".join(f"#{h}" for h in names)


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
    hashes, keywords = hashtags(params.get("hashtags")), tags(params.get("tags"))
    bad = [h for h in hashes if not _HASHTAG.match(h)]
    if bad:
        problems.append(
            f"a hashtag is one word of letters, digits or _, not {', '.join('#' + b for b in bad)}"
        )
    if len(hashes) > HASHTAGS_MAX:
        problems.append(f"{len(hashes)} hashtags, at most {HASHTAGS_MAX}")
    if (length := tags_length(keywords)) > TAGS_ROOM:
        problems.append(
            f"the tags take {length} characters, at most {TAGS_ROOM} with room for the retry tag"
        )
    text = str(params.get("description") or "").strip()
    size = len(description(text, hashes).encode())
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
        _field("hashtags", "Hashtags", hashtag_line(hashes), "The last line."),
        _field("tags", "Tags", ", ".join(keywords)),
    ]
    when = f"Private until then, then {visibility}." if schedule else "Right after confirming."
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
    run."""
    video = params.get("input_path") or ""
    text = description(str(values["description"]), hashtags(values.get("hashtags")))
    keywords = tags(values.get("tags"))
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


def result(connector: str, response: dict[str, Any]) -> dict[str, Any]:
    """What came back, normalized (`swarmify_connectors.publish.result`)."""
    url = response.get("url")
    return {
        "status": "scheduled" if response.get("publish_at") else "published",
        "links": {connector: url} if url else {},
        "id": response.get("video_id") or response.get("id"),
        "failed": {},
    }
