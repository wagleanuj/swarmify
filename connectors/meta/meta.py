"""The Meta connector: a stdio MCP server over Meta's Graph API (ADR 0015), posting Reels to
Instagram and a Facebook Page as a Connector Package (ADR 0019).

The account is the Manifest's world-scoped `meta` oauth field, finished by this package (ADR
0029): Swarmify sends the operator to Facebook Login for Business, exchanges the code, then calls
`signed_in` with the short-lived user token and the Meta app's client ID and secret. `signed_in`
swaps it for a long-lived user token (`fb_exchange_token`), lists the person's Pages
(`me/accounts`) and answers the token of the one Page with a linked Instagram account. A Page
token made from a long-lived user token never expires, so it is kept with no expiry and never
refreshed. With several such Pages the `page` Setting (META_PAGE) names one by name or ID.

Every later call starts with that Page token as META_ACCESS_TOKEN and reads the Page ID and its
Instagram account ID with it (`account`). An answer that the token is invalid (code 190, or an
OAuthException) fails the tool with `auth_failed`, which marks the field for sign-in again.

`preview` answers the fields a publish stage will post and every problem that would stop it,
each Platform's named; the core shows them at the gate and sends back what was confirmed. The
caption is the description (or the title without one), a blank line, then the hashtags on one
line; the metadata file's `title`, `description` and `hashtags` win over the params, the gate's
edits of `caption` and `hashtags` over all, and the connector adds no text of its own.

`publish` posts exactly the confirmed fields to each Platform and answers each one's link
(`links`) and failure (`failed`); it fails only when no Platform posted. On Instagram: a REELS
container with the caption, `share_to_feed`, `thumb_offset` (`cover_at` in ms) and, for a Trial
Reel, `trial_params`, uploaded resumably (the file itself to rupload.facebook.com, no public URL),
polled until its `status_code` is FINISHED, then `media_publish`, then its `permalink`. On the
Facebook Page: the Page's Reels upload, `video_reels` with `upload_phase=start`, the file to
rupload.facebook.com, then `upload_phase=finish` with the description and a `video_state` of
PUBLISHED, DRAFT or SCHEDULED (with `scheduled_publish_time`).

Meta has no idempotency key, so a retried stage (the same `idempotency_key`) reads a record the
first try left in the stage's folder of the run, `meta-<key>.json`: each Platform's container or
video ID, kept once its file is uploaded, and its post once finished. A finished post is answered
as recorded. A recorded Instagram container is published (FINISHED), or found among the account's
newest posts by its caption (PUBLISHED, its media ID being lost), or made again (EXPIRED). A
recorded Facebook video is answered when its `publishing_phase` is complete, else uploaded to
again when its upload never completed, then finished. A new key (a stage visited again) starts a
new record.

`outcomes` (the Capability) answers each post's `instagram_views` (the media insights metric
`views`, which replaced Reels `plays`), for a Reel also `instagram_reach` and
`instagram_avg_watch_seconds` (`ig_reels_avg_watch_time`), `facebook_views` (the Reel's
`fb_reels_total_plays`) and their sum `views`, for every post with an Instagram or Facebook link,
whichever connector posted it. An Instagram link names a shortcode, not a media ID, so the
account's latest media are listed to find it."""

import json
import os
import re
import struct
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

GRAPH = "https://graph.facebook.com/v26.0"
TOKEN = "META_ACCESS_TOKEN"
PAGE = "META_PAGE"
PAGE_FIELDS = "id,name,access_token,instagram_business_account"
RUPLOAD = "https://rupload.facebook.com"

PLATFORMS = ("instagram", "facebook")
VISIBILITIES = ("public", "draft")
EDITABLE = ("caption", "hashtags")
CAPTION_MAX = 2_200  # Instagram's limits for a caption, its hashtags and @mentions
HASHTAGS_MAX = 30
MENTIONS_MAX = 20
_HASHTAG = re.compile(r"^\w+$")
_TAGGED = re.compile(r"(?<!\w)#\w+")
_MENTION = re.compile(r"(?<!\w)@[\w.]+")
REEL_SECONDS = 90  # Facebook's limits for a Page Reel and how far ahead it may be scheduled
SCHEDULE_MIN = timedelta(minutes=10)
SCHEDULE_MAX = timedelta(days=29)
# Meta suggests polling a container about once a minute for up to 5 minutes; every 10 seconds
# answers a short Reel sooner, and 10 minutes leaves room for a long one.
POLL_SECONDS = 10
POLL_TRIES = 60

mcp = MCPServer("meta")


# The Graph API


def _error(response: httpx.Response, what: str) -> ToolError:
    try:
        detail = response.json().get("error", {})
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {"message": str(detail)}
    message = detail.get("message") or response.text[:300]
    if detail.get("code") == 190 or detail.get("type") == "OAuthException":
        # the core marks the account for sign-in again
        return ToolError(f"auth_failed: {what} failed, Meta refused the sign-in: {message}")
    return ToolError(f"{what} failed ({response.status_code}): {message}")


def _call(
    http: httpx.Client,
    method: str,
    path: str,
    what: str,
    token: str = "",
    params: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A Graph call, its JSON answer: a GET's `params` go in the query, a POST's as a form.
    Tokens go in a header or the query, never in an error."""
    headers = {"Authorization": f"OAuth {token}"} if token else {}
    query, form = (params, None) if method == "GET" else (None, params)
    try:
        response = http.request(method, f"{GRAPH}/{path}", params=query, data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise ToolError(f"{what} failed: Meta could not be reached") from exc
    if response.status_code != 200:
        raise _error(response, what)
    return response.json()


def _get(
    http: httpx.Client, path: str, what: str, token: str = "", **params: str
) -> dict[str, Any]:
    return _call(http, "GET", path, what, token, params)


def _named(page: Mapping[str, Any]) -> str:
    return f"{page.get('name')} ({page.get('id')})"


def page_token(
    http: httpx.Client,
    env: Mapping[str, str],
    access_token: str,
    client_id: str,
    client_secret: str,
) -> str:
    """The token of the Page to post as: the one with a linked Instagram account, or the one
    META_PAGE names."""
    if not access_token:
        raise ToolError("auth_failed: Meta answered the sign-in with no access token")
    long_lived = _get(
        http,
        "oauth/access_token",
        "exchanging for a long-lived token",
        grant_type="fb_exchange_token",
        client_id=client_id,
        client_secret=client_secret,
        fb_exchange_token=access_token,
    )["access_token"]
    # ponytail: the first 100 Pages; follow paging.next for an account that manages more.
    pages = _get(
        http, "me/accounts", "listing your Pages", long_lived, fields=PAGE_FIELDS, limit="100"
    ).get("data", [])
    if not pages:
        raise ToolError("this Facebook account manages no Page; sign in with one that does")
    wanted = env.get(PAGE, "").strip()
    if wanted:
        chosen = [p for p in pages if wanted in (p.get("id"), p.get("name"))]
        if not chosen:
            raise ToolError(
                f"no Page {wanted!r} among this account's Pages: "
                f"{', '.join(_named(p) for p in pages)}"
            )
        if not chosen[0].get("instagram_business_account"):
            raise ToolError(
                f"the Page {_named(chosen[0])} has no linked Instagram account; link one in"
                " the Page's settings, then sign in again"
            )
        return str(chosen[0]["access_token"])
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise ToolError(
            f"none of this account's Pages has a linked Instagram account: "
            f"{', '.join(_named(p) for p in pages)}; link one in the Page's settings, then sign"
            " in again"
        )
    if len(linked) > 1:
        raise ToolError(
            f"{len(linked)} Pages have a linked Instagram account: "
            f"{', '.join(_named(p) for p in linked)}; set the Facebook Page Setting to the name"
            " or ID of the one to post to, then sign in again"
        )
    return str(linked[0]["access_token"])


@mcp.tool(
    description="Finish the Meta Sign-in: keep the token of the Page to post as, which never"
    " expires (ADR 0029)."
)
def signed_in(access_token: str, client_id: str = "", client_secret: str = "") -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as http:
        token = page_token(http, os.environ, access_token, client_id, client_secret)
    return {"access_token": token, "expires_at": None}


def account_of(http: httpx.Client, env: Mapping[str, str]) -> dict[str, str | None]:
    """The Page the token is for and its linked Instagram account (None without one)."""
    if not env.get(TOKEN):
        raise ToolError("auth_failed: sign in to Meta on the Connectors page")
    me = _get(http, "me", "reading the Page", env[TOKEN], fields="id,instagram_business_account")
    instagram = (me.get("instagram_business_account") or {}).get("id")
    return {"page": str(me["id"]), "instagram": str(instagram) if instagram else None}


@mcp.tool(description="The Facebook Page the connector posts as and its Instagram account.")
def account() -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as http:
        return account_of(http, os.environ)


# What goes out: the preview a gate shows


def _words(value: Any) -> list[str]:
    """A list, or one string split on spaces and commas, without empties."""
    raw = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    return [w for w in (str(v).strip() for v in raw) if w]


def hashtags(value: Any) -> list[str]:
    """Each hashtag without its `#`."""
    return [w.lstrip("#") for w in _words(value) if w.lstrip("#")]


def hashtag_line(names: list[str]) -> str:
    return " ".join(f"#{h}" for h in names)


def full_caption(text: str, tagged: list[str]) -> str:
    """The caption as it goes out: the text, a blank line, then the hashtags. Nothing else."""
    line = hashtag_line(tagged)
    return f"{text.strip()}\n\n{line}" if text.strip() and line else text.strip() or line


_RELATIVE = re.compile(r"^(?:\+(\d+)d\s+)?([01]?\d|2[0-3]):([0-5]\d)(?:\s+(\S+))?$")


def _schedule(value: Any, now: datetime) -> tuple[str | None, str | None]:
    """(UTC ISO time, problem), in the YouTube connector's syntax: ISO 8601 with a timezone, or
    `HH:MM` (the next such time) or `+Nd HH:MM`, either with an optional IANA timezone."""
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


def _instagram_problems(caption: str, tagged: list[str], params: Mapping[str, Any]) -> list[str]:
    problems = []
    if len(caption) > CAPTION_MAX:
        problems.append(f"the caption is {len(caption)} characters, the limit is {CAPTION_MAX}")
    if (count := len(_TAGGED.findall(caption))) > HASHTAGS_MAX:
        problems.append(f"{count} hashtags, at most {HASHTAGS_MAX}")
    if bad := [h for h in tagged if not _HASHTAG.match(h)]:
        problems.append(
            f"a hashtag is one word of letters, digits or _, not {', '.join('#' + b for b in bad)}"
        )
    if (count := len(_MENTION.findall(caption))) > MENTIONS_MAX:
        problems.append(f"{count} @mentions, at most {MENTIONS_MAX}")
    if params.get("schedule"):
        problems.append("Instagram cannot schedule a post; remove the schedule")
    if params.get("visibility") == "draft":
        problems.append("Instagram has no drafts; a draft is Facebook's only")
    return problems


def seconds_of(video: Path) -> float | None:
    """An MP4's length from its `moov/mvhd` box, None when it has none."""
    with video.open("rb") as f:
        end, inside = video.stat().st_size, False
        while f.tell() + 8 <= end:
            start = f.tell()
            size, kind = struct.unpack(">I4s", f.read(8))
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
            elif size == 0:
                size = end - start
            if kind == b"moov" and not inside:
                end, inside = start + size, True  # read the boxes inside it
                continue
            if kind == b"mvhd" and inside:
                version = f.read(4)[0]
                layout = ">16xIQ" if version == 1 else ">8xII"
                scale, length = struct.unpack(layout, f.read(struct.calcsize(layout)))
                return length / scale if scale else None
            if size < 8:
                return None
            f.seek(start + size)
    return None


def _too_long(video: Path) -> str | None:
    seconds = seconds_of(video)
    if seconds is not None and seconds > REEL_SECONDS:
        return f"the video is {seconds:.0f} seconds, a Facebook Reel is at most {REEL_SECONDS}"
    return None


def _facebook_problems(
    schedule: str | None, params: Mapping[str, Any], inputs: list[str], now: datetime
) -> list[str]:
    problems = []
    if schedule:
        when = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
        if when <= now:
            problems.append(f"the schedule {schedule} has already passed")
        elif not now + SCHEDULE_MIN <= when <= now + SCHEDULE_MAX:
            problems.append(f"the schedule {schedule} is not 10 minutes to 29 days ahead")
        if params.get("visibility") == "draft":
            problems.append("a draft cannot be scheduled; remove the schedule or the draft")
    # The core names the input by its file name, so it is measured here only when it can be
    # read; publish measures it again before posting to Facebook.
    if inputs and Path(inputs[0]).is_file() and (long := _too_long(Path(inputs[0]))):
        problems.append(long)
    return problems


def preview_of(args: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the stage will post, plus every problem that would stop it, each Platform's named."""
    metadata = args.get("metadata") or {}
    edits = {k: v for k, v in (args.get("edits") or {}).items() if k in EDITABLE}
    params = {
        **(args.get("params") or {}),
        **{k: metadata[k] for k in ("title", "description", "hashtags") if metadata.get(k)},
    }
    raw = params.get("platforms") or list(PLATFORMS)
    platforms = [str(p) for p in (raw if isinstance(raw, list) else [raw])]
    visibility = str(params.get("visibility") or "public")
    text = str(params.get("description") or params.get("title") or "").strip()
    text = str(edits.get("caption", text)).strip()
    tagged = hashtags(edits.get("hashtags", params.get("hashtags")))
    caption = full_caption(text, tagged)
    now = now or datetime.now(UTC)
    schedule, bad_schedule = _schedule(params.get("schedule"), now)
    trial, share, cover = params.get("trial", False), params.get("share_to_feed", True), None
    problems = [bad_schedule] if bad_schedule else []
    if unknown := [p for p in platforms if p not in PLATFORMS]:
        problems.append(f"platforms are {' and '.join(PLATFORMS)}, got {', '.join(unknown)}")
    if visibility not in VISIBILITIES:
        problems.append(f"visibility is one of {', '.join(VISIBILITIES)}, got {visibility!r}")
    for key, value in (("trial", trial), ("share_to_feed", share)):
        if not isinstance(value, bool):
            problems.append(f"{key} is true or false, got {value!r}")
    if params.get("cover_at") is not None:
        cover = params["cover_at"]
        if isinstance(cover, bool) or not isinstance(cover, int | float) or cover < 0:
            problems.append(f"cover_at is a number of seconds, got {cover!r}")
    if "instagram" in platforms:
        problems += [f"Instagram: {p}" for p in _instagram_problems(caption, tagged, params)]
    inputs = [str(i) for i in args.get("inputs") or []]
    if "facebook" in platforms:
        problems += [f"Facebook: {p}" for p in _facebook_problems(schedule, params, inputs, now)]
    if not inputs:
        problems.append("a publish stage needs an input: the video to post")
    fields = [
        _field("platforms", "Platforms", platforms),
        _field("caption", "Caption", text, "The description, or the title without one."),
        _field("hashtags", "Hashtags", hashtag_line(tagged), "After a blank line."),
        _field("visibility", "Visibility", visibility),
        _field("schedule", "When", schedule, "" if schedule else "Right after confirming."),
        _field("trial", "Trial Reel", trial is True, "Shown to non-followers first."),
        _field("share_to_feed", "Share to feed", share is not False),
        _field("cover_at", "Cover at (seconds)", cover),
        _field("video", "Video", inputs[0] if inputs else None),
    ]
    return {"fields": fields, "problems": problems}


@mcp.tool(
    description="The fields a publish stage will post to Meta and every problem that would stop"
    " it, by Platform, with the gate's edits applied."
)
def preview(
    inputs: list[str] | None = None,
    params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return preview_of({"inputs": inputs, "params": params, "metadata": metadata, "edits": edits})


# Instagram: a Reels container, the file through Meta's resumable upload, then media_publish


def upload(http: httpx.Client, token: str, uri: str, video: Path) -> None:
    """The file itself, in one piece, to the rupload.facebook.com URI Meta gave."""
    size = video.stat().st_size
    with video.open("rb") as body:
        try:
            sent = http.post(
                uri,
                content=body,
                headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
            )
        except httpx.HTTPError as exc:
            raise ToolError("uploading the video failed: Meta could not be reached") from exc
    if sent.status_code != 200:
        raise _error(sent, "uploading the video")


Keep = Callable[..., None]  # records what a Platform has so far, by name: container, id, url


def _status(http: httpx.Client, token: str, container: str) -> dict[str, Any]:
    return _call(http, "GET", container, "waiting for the Reel", token, {"fields": "status_code"})


def _processed(http: httpx.Client, token: str, container: str) -> str:
    """Wait for the container: FINISHED, or PUBLISHED when an earlier try published it."""
    for attempt in range(POLL_TRIES):
        status = _status(http, token, container)
        state = status.get("status_code")
        if state in ("FINISHED", "PUBLISHED"):
            return str(state)
        if state in ("ERROR", "EXPIRED"):
            detail = f": {status['status']}" if status.get("status") else ""
            raise ToolError(f"Instagram could not process the video ({state}){detail}")
        if attempt < POLL_TRIES - 1:
            time.sleep(POLL_SECONDS)
    raise ToolError(
        f"Instagram was still processing the video after {POLL_TRIES * POLL_SECONDS} seconds"
    )


def _published_from(http: httpx.Client, token: str, instagram: str, caption: str) -> str:
    """The media an earlier try published, Meta having kept no link from container to media:
    the newest post with this caption."""
    # ponytail: the newest 25 posts; a retry comes minutes after, well within them.
    newest = _call(
        http,
        "GET",
        f"{instagram}/media",
        "finding the published Reel",
        token,
        {"fields": "id,caption", "limit": "25"},
    ).get("data", [])
    for media in newest:
        if media.get("caption") == caption:
            return str(media["id"])
    raise ToolError("Instagram published the Reel, but it is not among the account's newest posts")


def post_to_instagram(
    http: httpx.Client,
    token: str,
    instagram: str,
    video: Path,
    fields: Mapping[str, Any],
    kept: Mapping[str, str],
    keep: Keep,
) -> dict[str, str]:
    """Post `video` as a Reel to the Instagram account, or finish what `kept` recorded of an
    earlier try; answers its media `id` and `url`."""
    caption = full_caption(str(fields.get("caption") or ""), hashtags(fields.get("hashtags")))
    if media := kept.get("id"):
        if kept.get("url"):
            return {"id": media, "url": kept["url"]}
    else:
        created = kept.get("container")
        if created and _status(http, token, created).get("status_code") == "EXPIRED":
            created = None  # unpublished for 24 hours: made again
        if not created:
            form = {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "false" if fields.get("share_to_feed") is False else "true",
            }
            if fields.get("cover_at") is not None:
                form["thumb_offset"] = str(round(float(fields["cover_at"]) * 1000))  # in ms
            if fields.get("trial") is True:
                # MANUAL: it stays a Trial Reel until someone shares it to followers in the app
                form["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
            container = _call(http, "POST", f"{instagram}/media", "creating the Reel", token, form)
            created = str(container["id"])
            uri = container.get("uri") or f"{RUPLOAD}/ig-api-upload/v26.0/{created}"
            upload(http, token, uri, video)
            keep(container=created)
        if _processed(http, token, created) == "PUBLISHED":
            media = _published_from(http, token, instagram, caption)
        else:
            media = str(
                _call(
                    http,
                    "POST",
                    f"{instagram}/media_publish",
                    "publishing the Reel",
                    token,
                    {"creation_id": created},
                )["id"]
            )
        keep(id=media)
    link = _call(http, "GET", media, "reading the Reel's link", token, {"fields": "permalink"})
    url = str(link.get("permalink") or "")
    keep(url=url)
    return {"id": media, "url": url}


# The Facebook Page: the Page's Reels upload, start, the file, then finish


def post_to_facebook(
    http: httpx.Client,
    token: str,
    page: str,
    video: Path,
    fields: Mapping[str, Any],
    kept: Mapping[str, str],
    keep: Keep,
) -> dict[str, str]:
    """Post `video` as a Reel to the Page, published, as a draft or scheduled, or finish what
    `kept` recorded of an earlier try; answers its video `id` and `url`."""
    if kept.get("url"):
        return {"id": kept["video"], "url": kept["url"]}
    if long := _too_long(video):
        raise ToolError(long)
    path, what = f"{page}/video_reels", "posting the Reel to the Page"
    reel, uri, status = kept.get("video"), None, {}
    if reel:
        status = _call(http, "GET", reel, "reading the Reel", token, {"fields": "status"})
        status = status.get("status") or {}
        if (status.get("publishing_phase") or {}).get("status") == "complete":
            url = f"https://www.facebook.com/reel/{reel}"
            keep(url=url)
            return {"id": reel, "url": url}
    else:
        started = _call(http, "POST", path, what, token, {"upload_phase": "start"})
        reel, uri = str(started["video_id"]), started.get("upload_url")
        keep(video=reel)
    if (status.get("uploading_phase") or {}).get("status") != "complete":
        upload(http, token, uri or f"{RUPLOAD}/video-upload/v26.0/{reel}", video)
    form = {
        "upload_phase": "finish",
        "video_id": reel,
        "video_state": "DRAFT" if fields.get("visibility") == "draft" else "PUBLISHED",
        "description": full_caption(
            str(fields.get("caption") or ""), hashtags(fields.get("hashtags"))
        ),
    }
    if fields.get("schedule"):
        when = datetime.fromisoformat(str(fields["schedule"]).replace("Z", "+00:00"))
        form["video_state"], form["scheduled_publish_time"] = (
            "SCHEDULED",
            str(int(when.timestamp())),
        )
    if _call(http, "POST", path, what, token, form).get("success") is False:
        raise ToolError("Facebook did not accept the Reel")
    url = f"https://www.facebook.com/reel/{reel}"
    keep(url=url)
    return {"id": reel, "url": url}


def record_of(input_path: Path, idempotency_key: str) -> Path | None:
    """Where a stage keeps what it posted: `meta-<key>.json` in its folder of the run its input
    is in (`<world>/runs/<run>/<stage>`, the one folder it may write). The key is the stage id,
    or `<stage id>.<visit>` for a stage visited again. None outside a run."""
    run = next((p for p in input_path.parents if p.parent.name == "runs"), None)
    if not idempotency_key or run is None:
        return None
    stage = idempotency_key
    if not (run / stage).is_dir():
        stage = idempotency_key.rpartition(".")[0] or idempotency_key
    folder = run / stage
    return folder / f"meta-{idempotency_key}.json" if folder.is_dir() else None


def publish_fields(
    http: httpx.Client,
    env: Mapping[str, str],
    fields: Mapping[str, Any],
    input_path: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Post exactly the confirmed `fields` to each Platform, finishing what an earlier try under
    `idempotency_key` recorded. One Platform failing leaves the other's post standing: the answer
    lists it under `failed`. When none posted, it fails."""
    video = Path(input_path)
    if not video.is_file():
        raise ToolError(f"the video {video} does not exist")
    who = account_of(http, env)
    path = record_of(video, idempotency_key)
    record: dict[str, dict[str, str]] = (
        json.loads(path.read_text()) if path and path.is_file() else {}
    )
    links: dict[str, str] = {}
    failed: dict[str, str] = {}
    post_id = None
    for platform in fields.get("platforms") or PLATFORMS:
        kept = record.setdefault(platform, {})

        def keep(kept: dict[str, str] = kept, **values: str) -> None:
            kept.update(values)
            if path:
                path.write_text(json.dumps(record))

        try:
            if platform == "facebook":
                page = str(who["page"])
                posted = post_to_facebook(http, env[TOKEN], page, video, fields, kept, keep)
            elif not who["instagram"]:
                raise ToolError("the Page has no linked Instagram account")
            else:
                instagram = who["instagram"]
                posted = post_to_instagram(http, env[TOKEN], instagram, video, fields, kept, keep)
            links[platform], post_id = posted["url"], post_id or posted["id"]
        except ToolError as exc:
            failed[platform] = str(exc)
    if not links:
        raise ToolError("; ".join(f"{p}: {e}" for p, e in failed.items()))
    when = fields.get("schedule")
    return {
        "status": "scheduled" if when else "published",
        "links": links,
        "id": post_id,
        "failed": failed,
        "public": fields.get("visibility") != "draft",  # a draft only the Page's admins see
        **({"scheduled_for": str(when)} if when else {}),
    }


@mcp.tool(
    description="Post the video at `input_path` as a Reel to each of the confirmed `fields`'"
    " Platforms, answering each one's link and any failures."
)
def publish(fields: dict[str, Any], input_path: str, idempotency_key: str = "") -> dict[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(60.0, write=None)) as http:
        return publish_fields(http, os.environ, fields, input_path, idempotency_key)


# Outcomes: views on each Platform, for every post with an Instagram or Facebook link


_INSTAGRAM = re.compile(r"^https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)")
_FACEBOOK = re.compile(
    r"^https?://(?:www\.|m\.)?facebook\.com/(?:reel/|[^?#]*/videos/|watch/?\?v=)(\w+)"
)
REEL_METRICS = ("views", "reach", "ig_reels_avg_watch_time")
"""A Reel's retention. Graph v22 folded Reels `plays` into `views`."""
MEDIA_PAGES = 10  # how far back the Instagram account's media are searched for a post's link


def claimed(posts: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Each post's Instagram shortcode and Facebook video ID, by post id: from its links,
    whichever connector posted it."""
    out = {}
    for post in posts:
        links = post.get("links")
        found: dict[str, str] = {}
        for url in links.values() if isinstance(links, dict) else []:
            if m := _INSTAGRAM.match(str(url)):
                found.setdefault("instagram", m[1])
            elif m := _FACEBOOK.match(str(url)):
                found.setdefault("facebook", m[1])
        if found and post.get("id"):
            out[str(post["id"])] = found
    return out


def _value(answer: Mapping[str, Any]) -> int:
    """A lifetime metric's value; Meta answers an empty set while it has none yet."""
    for metric in answer.get("data") or []:
        total = metric.get("total_value") or (metric.get("values") or [{}])[-1]
        return int(total.get("value") or 0)
    return 0


def _values(answer: Mapping[str, Any]) -> dict[str, int]:
    """Each lifetime metric's value by name."""
    out = {}
    for metric in answer.get("data") or []:
        total = metric.get("total_value") or (metric.get("values") or [{}])[-1]
        out[str(metric.get("name"))] = int(total.get("value") or 0)
    return out


def _quietly[T](read: Callable[[], T]) -> T | None:
    """A read's value, None when Meta fails it; a refused sign-in still fails the tool."""
    try:
        return read()
    except ToolError as exc:
        if str(exc).startswith("auth_failed"):
            raise
        return None


def media_ids(http: httpx.Client, token: str, instagram: str, codes: set[str]) -> dict[str, str]:
    """The media ID of each shortcode among the Instagram account's latest media."""
    found: dict[str, str] = {}
    after = ""
    # ponytail: the latest MEDIA_PAGES * 100 media; older posts' Outcomes are left out
    for _ in range(MEDIA_PAGES):
        params = {"fields": "id,permalink", "limit": "100", **({"after": after} if after else {})}
        listed = _call(http, "GET", f"{instagram}/media", "listing the Reels", token, params)
        for media in listed.get("data", []):
            if (m := _INSTAGRAM.match(str(media.get("permalink")))) and m[1] in codes:
                found[m[1]] = str(media["id"])
        paging = listed.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after", "")
        if codes <= found.keys() or not paging.get("next") or not after:
            return found
    return found


def outcomes_of(http: httpx.Client, env: Mapping[str, str], posts: list[dict[str, Any]]) -> dict:
    """`instagram_views`, `facebook_views` and their sum `views` of every post with an Instagram
    or Facebook link, by post id. Meta failing for one post leaves it out, not the others."""
    wanted = claimed(posts)
    if not wanted:
        return {}
    who, token = account_of(http, env), env[TOKEN]
    codes = {w["instagram"] for w in wanted.values() if "instagram" in w}
    media = {}
    if codes and who["instagram"]:
        media = _quietly(lambda: media_ids(http, token, str(who["instagram"]), codes)) or {}

    def views(platform: str, thing: str) -> int | None:
        if platform == "instagram":
            if thing not in media:
                return None
            path, metric = f"{media[thing]}/insights", "views"
        else:
            path, metric = f"{thing}/video_insights", "fb_reels_total_plays"
        what = f"reading the {platform.title()} views"
        return _quietly(lambda: _value(_get(http, path, what, token, metric=metric)))

    def reel(code: str) -> dict[str, float] | None:
        """An Instagram Reel's views, reach and average watch time in one read; None when Meta
        refuses the Reels-only metrics, as for a photo post."""
        if code not in media:
            return None
        path, metric = f"{media[code]}/insights", ",".join(REEL_METRICS)
        what = "reading the Instagram Reel's insights"
        got = _quietly(lambda: _values(_get(http, path, what, token, metric=metric)))
        if got is None or "views" not in got:
            return None
        out: dict[str, float] = {"instagram_views": got["views"]}
        if "reach" in got:
            out["instagram_reach"] = got["reach"]
        if "ig_reels_avg_watch_time" in got:  # milliseconds
            out["instagram_avg_watch_seconds"] = round(got["ig_reels_avg_watch_time"] / 1000, 2)
        return out

    out = {}
    for post, things in wanted.items():
        found: dict[str, float] = {}
        for p, thing in things.items():
            if p == "instagram" and (measured := reel(thing)) is not None:
                found |= measured
            elif (n := views(p, thing)) is not None:
                found[f"{p}_views"] = n
        if found:
            total = sum(n for k, n in found.items() if k.endswith("_views"))
            out[post] = {"views": total, **found}
    return out


@mcp.tool(
    description="The Outcomes Capability: views on Instagram and Facebook of every post with a"
    " link to either, whoever posted it, by post id."
)
def outcomes(posts: list[dict[str, Any]]) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as http:
        return outcomes_of(http, os.environ, posts)


if __name__ == "__main__":  # pragma: no cover - started as a subprocess
    mcp.run("stdio")
