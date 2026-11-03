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
polled until its `status_code` is FINISHED, then `media_publish`, then its `permalink`. Posting to
the Facebook Page (#372), retries (#373) and `outcomes` (#374) are not built yet.
"""

import json
import os
import re
import time
from collections.abc import Mapping
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
    schedule, bad_schedule = _schedule(params.get("schedule"), now or datetime.now(UTC))
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
    if "facebook" in platforms:
        # ponytail: Instagram only until the Facebook Page's Reels upload is built (#372).
        problems.append("Facebook: posting to the Facebook Page is not built yet")
    inputs = args.get("inputs") or []
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


def post_to_instagram(
    http: httpx.Client, token: str, instagram: str, video: Path, fields: Mapping[str, Any]
) -> dict[str, str]:
    """Post `video` as a Reel to the Instagram account; answers its media `id` and `url`."""
    form = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": full_caption(str(fields.get("caption") or ""), hashtags(fields.get("hashtags"))),
        "share_to_feed": "false" if fields.get("share_to_feed") is False else "true",
    }
    if fields.get("cover_at") is not None:
        form["thumb_offset"] = str(round(float(fields["cover_at"]) * 1000))  # in ms
    if fields.get("trial") is True:
        # MANUAL: it stays a Trial Reel until someone shares it to followers in the app
        form["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
    container = _call(http, "POST", f"{instagram}/media", "creating the Reel", token, form)
    created = str(container["id"])
    size = video.stat().st_size
    uri = container.get("uri") or f"{RUPLOAD}/ig-api-upload/v26.0/{created}"
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
    for attempt in range(POLL_TRIES):
        status = _call(
            http, "GET", created, "waiting for the Reel", token, {"fields": "status_code"}
        )
        state = status.get("status_code")
        if state == "FINISHED":
            break
        if state in ("ERROR", "EXPIRED"):
            detail = f": {status['status']}" if status.get("status") else ""
            raise ToolError(f"Instagram could not process the video ({state}){detail}")
        if attempt < POLL_TRIES - 1:
            time.sleep(POLL_SECONDS)
    else:
        raise ToolError(
            f"Instagram was still processing the video after {POLL_TRIES * POLL_SECONDS} seconds"
        )
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
    link = _call(http, "GET", media, "reading the Reel's link", token, {"fields": "permalink"})
    return {"id": media, "url": str(link.get("permalink") or "")}


def publish_fields(
    http: httpx.Client, env: Mapping[str, str], fields: Mapping[str, Any], input_path: str
) -> dict[str, Any]:
    """Post exactly the confirmed `fields` to each Platform. One Platform failing leaves the
    other's post standing: the answer lists it under `failed`. When none posted, it fails."""
    video = Path(input_path)
    if not video.is_file():
        raise ToolError(f"the video {video} does not exist")
    who = account_of(http, env)
    links: dict[str, str] = {}
    failed: dict[str, str] = {}
    post_id = None
    for platform in fields.get("platforms") or PLATFORMS:
        try:
            if platform != "instagram":
                raise ToolError(f"posting to {platform} is not built yet")
            if not who["instagram"]:
                raise ToolError("the Page has no linked Instagram account")
            posted = post_to_instagram(http, env[TOKEN], who["instagram"], video, fields)
            links[platform], post_id = posted["url"], posted["id"]
        except ToolError as exc:
            failed[platform] = str(exc)
    if not links:
        raise ToolError("; ".join(f"{p}: {e}" for p, e in failed.items()))
    return {"status": "published", "links": links, "id": post_id, "failed": failed}


@mcp.tool(
    description="Post the video at `input_path` as a Reel to each of the confirmed `fields`'"
    " Platforms, answering each one's link and any failures."
)
def publish(fields: dict[str, Any], input_path: str, idempotency_key: str = "") -> dict[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(60.0, write=None)) as http:
        return publish_fields(http, os.environ, fields, input_path)


# Outcomes: a placeholder until #374


@mcp.tool(description="The Outcomes Capability: views of Meta posts, by post id.")
def outcomes(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {}


if __name__ == "__main__":  # pragma: no cover - started as a subprocess
    mcp.run("stdio")
