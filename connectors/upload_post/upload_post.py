"""The Upload-Post connector: a stdio MCP server over Upload-Post's HTTP API (ADR 0015) answering
the publish contract (`swarmify_connectors.publish`): `preview`, `publish`, the long job a
background upload becomes, and `check_key`, its Setup test.

`preview` takes the stage's params: `title`, `description`, `visibility` (public, unlisted or
private), `made_for_kids` (YouTube's COPPA flag, false by default), an optional `schedule` (ISO
8601 with a timezone, or relative, `HH:MM` (the next such time) or `+Nd HH:MM` (N days from now),
either with an optional IANA timezone after it, UTC by default), `user` (the Upload-Post profile)
and `platforms` (youtube by default). The metadata file's `title`, `description`, `hashtags` and
`tags` win over the params, and the gate's edits over all. `hashtags` (a list, or one string; `#`
optional) go out as the description's last line; `tags` (a list, or one comma-separated string)
go to YouTube as its tags. Nothing posts before a schedule, which needs `visibility: public`.

`publish` posts exactly the confirmed fields to `POST /upload`, asking to wait for the links, with
the stage's idempotency key as the Idempotency-Key header, so a retry within a day never posts
twice. Upload-Post still moves an upload over about 59 seconds to the background, answering a
`request_id`: `publish` then answers the long-job shape, `job_id` and `poll: upload_status`, and
the core polls `upload_status` until it is done. A scheduled post answers its Upload-Post job id.

It is a Connector Package (ADR 0019): `swarmify-connector.toml` beside it is everything Swarmify
knows of it; its API key arrives as `UPLOAD_POST_API_KEY`.
"""

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API = "https://api.upload-post.com/api"
VISIBILITIES = ("public", "unlisted", "private")
UNFINISHED = ("pending", "in_progress", "processing")  # the status endpoint while it works
TITLE_MAX = 100  # YouTube's limit, the tightest of the platforms we post to
DESCRIPTION_BYTES = 5_000  # YouTube's limit
HASHTAGS_MAX = 5  # well under YouTube's 60: more reads as spam (research 2026-10-30)
TAGS_CHARS = 500  # YouTube's limit for all tags together, commas and quotes included
# The youtube connector keeps 50 for its retry tag; the same room here moves a stage between them.
TAGS_ROOM = TAGS_CHARS - 50
EDITABLE = ("title", "description", "hashtags", "tags")
_HASHTAG = re.compile(r"^\w+$")

mcp = MCPServer("upload_post")


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


def hashtag_line(names: list[str]) -> str:
    return " ".join(f"#{h}" for h in names)


def description(text: str, tagged: list[str]) -> str:
    """The description as it goes out: the hashtags as its last line."""
    line = hashtag_line(tagged)
    return f"{text.strip()}\n\n{line}" if text.strip() and line else text.strip() or line


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


def preview_of(args: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
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
    raw = params.get("platforms") or ["youtube"]
    platforms = [str(p) for p in (raw if isinstance(raw, list) else [raw])]
    user = str(params.get("user") or "").strip()
    if not user:
        problems.append("upload_post needs params.user, the Upload-Post profile to post as")
    when = "Nothing posts before then." if schedule else "Right after confirming."
    return {
        "fields": [
            _field("title", "Title", title),
            _field("description", "Description", text),
            _field("hashtags", "Hashtags", hashtag_line(hashes), "The last line."),
            _field("tags", "Tags", ", ".join(keywords)),
            _field("platforms", "Platforms", platforms),
            _field("user", "Profile", user, "The Upload-Post profile it posts as."),
            _field("visibility", "Visibility", visibility),
            _field("made_for_kids", "Made for kids", made_for_kids is True),
            _field("schedule", "When", schedule, when),
            _field("video", "Video", inputs[0] if inputs else None),
        ],
        "problems": problems,
    }


def form(fields: dict[str, Any]) -> dict[str, Any]:
    """The upload form for the confirmed `fields`, in Upload-Post's names; YouTube's own fields
    only when posting there, as Upload-Post's SDK sends them."""
    platforms = [str(p) for p in fields["platforms"]]
    out: dict[str, Any] = {
        "user": str(fields["user"]).strip(),
        "title": fields["title"],
        "platform[]": platforms,
        "async_upload": "false",  # answer with the post links, not a request id to poll
    }
    if text := description(str(fields.get("description") or ""), hashtags(fields.get("hashtags"))):
        out["description"] = text
    if fields.get("schedule"):
        out["scheduled_date"] = fields["schedule"]
    if "youtube" in platforms:
        if keywords := tags(fields.get("tags")):
            out["tags[]"] = keywords
        out["privacyStatus"] = fields["visibility"]
        out["selfDeclaredMadeForKids"] = "true" if fields["made_for_kids"] else "false"
    return out


def result(body: dict[str, Any]) -> dict[str, Any]:
    """An upload or status answer, normalized as the publish contract's: `status` (published,
    scheduled or pending), `links` (platform to URL), `id` (the scheduler job or the background
    request) and `failed` (platform to error). A status answer lists `results` per platform, with
    `post_url` and `message` where the upload answer has `url` and `error`. One with no results
    that is `success: false` (an unknown or expired request) or a failed status is a failure,
    never an empty success."""
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


def _key() -> str:
    key = os.environ.get("UPLOAD_POST_API_KEY", "")
    if not key:
        raise ToolError("upload_post needs its API key: set it up on the Connectors page")
    return key


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The JSON body of a 2xx answer; else a ToolError carrying what Upload-Post said."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if not response.is_success:
        said = body.get("message") or body.get("detail") or body.get("error") or response.text
        raise ToolError(f"Upload-Post answered {response.status_code}: {said}"[:500])
    return body


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Apikey {_key()}"},
        timeout=httpx.Timeout(600, connect=10),  # a sync upload waits for every platform
    )


def publish_with(
    http: httpx.Client, fields: dict[str, Any], idempotency_key: str, input_path: str
) -> dict[str, Any]:
    video = Path(input_path)
    if not input_path or not video.is_file():
        raise ToolError(f"upload_post needs the video to post; {input_path!r} is no file")
    with video.open("rb") as f:
        response = http.post(
            "/upload",
            data=form(fields),
            files={"video": (video.name, f, "video/mp4")},
            headers={"Idempotency-Key": idempotency_key},
        )
    out = result(_answer(response))
    if out["status"] != "pending":
        return out
    return {
        "job_id": out["id"],
        "poll": "upload_status",
        "status": "running",
        "message": "Upload-Post is finishing the upload in the background",
    }


def status_with(http: httpx.Client, job_id: str) -> dict[str, Any]:
    out = result(_answer(http.get("/uploadposts/status", params={"request_id": job_id})))
    if out["status"] == "pending":
        return {"status": "running", "message": "Upload-Post is still uploading"}
    return {**out, "status": "done"}


def key_check_with(http: httpx.Client) -> dict[str, Any]:
    try:
        body = _answer(http.get("/uploadposts/me"))
    except ToolError as exc:
        return {"ok": False, "message": str(exc)}
    account = body.get("email") or (body.get("user") or {}).get("email")
    return {"ok": True, "message": f"Signed in to Upload-Post as {account}." if account else ""}


@mcp.tool(description="What a publish stage will post, and every problem that would stop it.")
def preview(
    inputs: list[str] | None = None,
    params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return preview_of({"inputs": inputs, "params": params, "metadata": metadata, "edits": edits})


@mcp.tool(description="Post exactly the confirmed fields; a background upload is a long job.")
def publish(fields: dict[str, Any], idempotency_key: str, input_path: str = "") -> dict[str, Any]:
    with _client() as http:
        return publish_with(http, fields, idempotency_key, input_path)


@mcp.tool(description="Poll a background upload until its links are known.")
def upload_status(job_id: str) -> dict[str, Any]:
    with _client() as http:
        return status_with(http, job_id)


@mcp.tool(description="Check the API key cheaply, naming the account it belongs to.")
def check_key() -> dict[str, Any]:
    with _client() as http:
        return key_check_with(http)


if __name__ == "__main__":
    mcp.run("stdio")
