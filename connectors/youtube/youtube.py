"""The YouTube connector: a stdio MCP server over the YouTube Data API (ADR 0015), answering the
publish contract (ADR 0007) as a Connector Package (ADR 0019).

Four tools. `preview` answers the fields a publish stage will post and every problem that would
stop it; the core shows them at the gate and sends back what was confirmed. `publish` uploads the
confirmed values with `videos.insert` (resumable upload, one request for the metadata and one for
the bytes) and answers the video's id and link. With a `schedule` the video is uploaded private
with `publishAt`, and YouTube makes it public at that time: `publishAt` can be set only while the
privacy status is private. `views` answers the view counts of up to 50 videos with one
`videos.list` call (part statistics, 1 quota unit): the outcome write-back. `channel` answers the
title of the channel the account posts to (`channels.list` mine, 1 quota unit).

The account is the Manifest's `youtube` oauth field: Swarmify signs in, keeps the token set and
starts this process with a current access token as YOUTUBE_ACCESS_TOKEN. A token Google refuses
fails the tool with `auth_failed`, which marks the field for sign-in again.

`preview` takes the stage's `inputs` (file names), `params`, `metadata` (the frontmatter of the
file `params.metadata` names) and `edits` (the gate's). Params: `title`, `description`,
`visibility` (public, unlisted or private), `made_for_kids` (YouTube's COPPA flag, false by
default) and an optional `schedule`: ISO 8601 with a timezone, or relative, `HH:MM` (the next such
time) or `+Nd HH:MM` (N days from now), either with an optional IANA timezone after it (UTC by
default). The metadata's `title`, `description`, `hashtags` and `tags` win over the params, and
the gate's edits over all. `hashtags` (a list, or one string; `#` optional) go out as the
description's last line, where YouTube shows up to three above the title; `tags` (a list, or one
comma-separated string) go to YouTube's tags beside the retry tag. A scheduled video stays
private until its time, then goes public, so a schedule needs `visibility: public`.

A retried publish stage calls again with the same `idempotency_key` (the stage id), and the
first call may have uploaded before it failed. So an upload is tagged `swarmify-<key>`, and
before uploading the connector reads the channel's latest 50 uploads (`channels.list` mine, then
`playlistItems.list` and `videos.list`, 3 quota units) and answers the tagged video if there is
one.
"""

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
TOKEN = "YOUTUBE_ACCESS_TOKEN"
MAX_IDS = 50

VISIBILITIES = ("public", "unlisted", "private")
TITLE_MAX = 100  # YouTube's limit
DESCRIPTION_BYTES = 5_000  # YouTube's limit
HASHTAGS_MAX = 5  # well under YouTube's 60: more reads as spam (research 2026-10-30)
TAGS_CHARS = 500  # YouTube's limit for all tags together, commas and quotes included
# Each upload is tagged swarmify-<stage id>.<visit>: about 45 characters with its comma.
TAGS_ROOM = TAGS_CHARS - 50
EDITABLE = ("title", "description", "hashtags", "tags")
_HASHTAG = re.compile(r"^\w+$")

mcp = MCPServer("youtube")


# What goes out


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


def full_description(text: str, tagged: list[str]) -> str:
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


def preview_of(args: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the stage will publish, plus every problem that would stop it."""
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
    size = len(full_description(text, hashes).encode())
    if size > DESCRIPTION_BYTES:
        problems.append(
            f"the description with its hashtags is {size} bytes, the limit is {DESCRIPTION_BYTES}"
        )
    inputs = args.get("inputs") or []
    if not inputs:
        problems.append("a publish stage needs an input: the video to post")
    when = f"Private until then, then {visibility}." if schedule else "Right after confirming."
    fields = [
        _field("title", "Title", title),
        _field("description", "Description", text),
        _field("hashtags", "Hashtags", hashtag_line(hashes), "The last line."),
        _field("tags", "Tags", ", ".join(keywords)),
        _field("visibility", "Visibility", visibility),
        _field("made_for_kids", "Made for kids", made_for_kids is True),
        _field("schedule", "When", schedule, when),
        _field("video", "Video", inputs[0] if inputs else None),
    ]
    return {"fields": fields, "problems": problems}


@mcp.tool(
    description="The fields a publish stage will post to YouTube and every problem that would"
    " stop it, with the gate's edits applied."
)
def preview(
    inputs: list[str] | None = None,
    params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return preview_of({"inputs": inputs, "params": params, "metadata": metadata, "edits": edits})


# The Data API


def _error(response: httpx.Response, what: str) -> ToolError:
    try:
        detail = response.json().get("error", {})
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
    except ValueError:
        message = response.text[:500]
    if response.status_code == 401:  # the core marks the account for sign-in again
        return ToolError(f"auth_failed: {what} failed, Google refused the sign-in: {message}")
    return ToolError(f"{what} failed ({response.status_code}): {message}")


def _headers(env: Mapping[str, str]) -> dict[str, str]:
    if not env.get(TOKEN):
        raise ToolError("auth_failed: sign in to YouTube on the Connectors page")
    return {"Authorization": f"Bearer {env[TOKEN]}"}


def uploaded_under(http: httpx.Client, auth: dict[str, str], tag: str) -> str | None:
    """The id of the channel's video tagged `tag`, among its latest 50 uploads."""
    # ponytail: only the latest 50 uploads; a retry comes soon after the call it repeats.

    def items(url: str, what: str, **params: str) -> list[dict[str, Any]]:
        response = http.get(url, params=params, headers=auth)
        if response.status_code == 404:  # a channel with no uploads has no uploads playlist
            return []
        if response.status_code != 200:
            raise _error(response, what)
        return response.json().get("items", [])

    channels = items(
        CHANNELS_URL, "finding the channel's uploads", part="contentDetails", mine="true"
    )
    if not channels:
        return None
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    latest = items(
        PLAYLIST_ITEMS_URL,
        "listing the channel's uploads",
        part="contentDetails",
        playlistId=playlist,
        maxResults=str(MAX_IDS),
    )
    ids = [i["contentDetails"]["videoId"] for i in latest]
    if not ids:
        return None
    for video in items(VIDEOS_URL, "reading upload tags", part="snippet", id=",".join(ids)):
        if tag in video.get("snippet", {}).get("tags", []):
            return str(video["id"])
    return None


def upload(
    http: httpx.Client,
    env: Mapping[str, str],
    video: Path,
    title: str,
    description: str = "",
    visibility: str = "private",
    schedule: str = "",
    made_for_kids: bool = False,
    idempotency_key: str = "",
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Upload `video`, or find the one an earlier try under `idempotency_key` uploaded. Answers
    the publish contract's `id`, `url` and `status`."""
    if not video.is_file():
        raise ToolError(f"the video {video} does not exist")
    status: dict[str, Any] = {"privacyStatus": visibility, "selfDeclaredMadeForKids": made_for_kids}
    if schedule:
        status = {**status, "privacyStatus": "private", "publishAt": schedule}
    auth = _headers(env)

    def answer(video_id: str) -> dict[str, Any]:
        return {
            "id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "status": "scheduled" if schedule else "published",
        }

    snippet: dict[str, Any] = {"title": title, "description": description}
    keywords = list(keywords or [])
    if idempotency_key:
        tag = f"swarmify-{idempotency_key}"
        if done := uploaded_under(http, auth, tag):
            return answer(done)
        keywords.append(tag)  # beside the real tags, never instead
    if keywords:
        snippet["tags"] = keywords
    size = video.stat().st_size
    started = http.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={**auth, "X-Upload-Content-Type": "video/*", "X-Upload-Content-Length": str(size)},
        json={"snippet": snippet, "status": status},
    )
    if started.status_code != 200 or "location" not in started.headers:
        raise _error(started, "starting the YouTube upload")
    # ponytail: one PUT of the whole file; resume an interrupted upload by range when videos
    # get big enough for a dropped connection to matter.
    with video.open("rb") as body:
        sent = http.put(
            started.headers["location"],
            content=body,
            headers={**auth, "Content-Type": "video/*", "Content-Length": str(size)},
        )
    if sent.status_code not in (200, 201):
        raise _error(sent, "uploading the video")
    return answer(sent.json()["id"])


def publish_fields(
    http: httpx.Client,
    env: Mapping[str, str],
    fields: Mapping[str, Any],
    input_path: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Post exactly the confirmed `fields`: the hashtags as the description's last line, the
    tags beside the retry tag."""
    return upload(
        http,
        env,
        Path(input_path),
        str(fields["title"]),
        full_description(str(fields.get("description") or ""), hashtags(fields.get("hashtags"))),
        str(fields.get("visibility") or "private"),
        str(fields.get("schedule") or ""),
        fields.get("made_for_kids") is True,
        idempotency_key,
        tags(fields.get("tags")),
    )


@mcp.tool(
    description="Upload the video at `input_path` to YouTube with the `fields` a gate confirmed."
    " With a schedule it stays private until then and goes public at that time."
)
def publish(fields: dict[str, Any], input_path: str, idempotency_key: str = "") -> dict[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(60.0, write=None)) as http:
        return publish_fields(http, os.environ, fields, input_path, idempotency_key)


def video_views(http: httpx.Client, env: Mapping[str, str], ids: list[str]) -> dict[str, int]:
    """View counts by video id; a video YouTube no longer has (deleted, private) is left out."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    auth, counts = _headers(env), {}
    for start in range(0, len(ids), MAX_IDS):
        response = http.get(
            VIDEOS_URL,
            params={"part": "statistics", "id": ",".join(ids[start : start + MAX_IDS])},
            headers=auth,
        )
        if response.status_code != 200:
            raise _error(response, "reading YouTube view counts")
        for item in response.json().get("items", []):
            counts[item["id"]] = int(item.get("statistics", {}).get("viewCount", 0))
    return counts


@mcp.tool(description="View counts of YouTube videos by id, for the outcome write-back.")
def views(video_ids: list[str]) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as http:
        return {"views": video_views(http, os.environ, video_ids)}


def channel_name(http: httpx.Client, env: Mapping[str, str]) -> str:
    """The title of the channel the signed-in account posts to."""
    response = http.get(
        CHANNELS_URL, params={"part": "snippet", "mine": "true"}, headers=_headers(env)
    )
    if response.status_code != 200:
        raise _error(response, "reading the YouTube channel")
    items = response.json().get("items", [])
    if not items:
        raise ToolError("this Google account has no YouTube channel")
    return str(items[0]["snippet"]["title"])


@mcp.tool(description="The channel the signed-in account posts to.")
def channel() -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as http:
        return {"channel": channel_name(http, os.environ)}


if __name__ == "__main__":  # pragma: no cover - started as a subprocess
    mcp.run("stdio")
