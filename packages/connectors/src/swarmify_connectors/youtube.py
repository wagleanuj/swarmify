"""The YouTube publish connector: a stdio MCP server over the YouTube Data API (ADR 0015).

One tool, `youtube`, uploads a video with `videos.insert` (resumable upload, one request for the
metadata and one for the bytes) and answers with its id and link. With a `schedule` the video is
uploaded private with `publishAt`, and YouTube makes it public at that time: `publishAt` can be
set only while the privacy status is private.

Credentials, from `~/.swarmify/.env`: YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET of a Google
Cloud OAuth client (type Desktop app, YouTube Data API v3 enabled), and YOUTUBE_REFRESH_TOKEN,
which `uv run python -m swarmify_connectors.youtube authorize` prints after a browser consent.

The swarmify side never repeats a publish call whose outcome is unknown, so this wrapper keeps
no state of its own; `idempotency_key` is accepted and ignored.
"""

import os
import secrets
import sys
import urllib.parse
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CREDENTIALS = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]

mcp = MCPServer("youtube")


def _error(response: httpx.Response, what: str) -> ToolError:
    try:
        detail = response.json().get("error", {})
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
    except ValueError:
        message = response.text[:500]
    return ToolError(f"{what} failed ({response.status_code}): {message}")


def access_token(http: httpx.Client, env: Mapping[str, str]) -> str:
    missing = [n for n in CREDENTIALS if not env.get(n)]
    if missing:
        raise ToolError(f"youtube needs {', '.join(missing)}")
    response = http.post(
        TOKEN_URL,
        data={
            "client_id": env["YOUTUBE_CLIENT_ID"],
            "client_secret": env["YOUTUBE_CLIENT_SECRET"],
            "refresh_token": env["YOUTUBE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
    )
    if response.status_code != 200:
        raise _error(response, "refreshing the YouTube token")
    return response.json()["access_token"]


def publish(
    http: httpx.Client,
    env: Mapping[str, str],
    video: Path,
    title: str,
    description: str = "",
    visibility: str = "private",
    schedule: str = "",
) -> dict[str, Any]:
    if not video.is_file():
        raise ToolError(f"the video {video} does not exist")
    status: dict[str, Any] = {"privacyStatus": visibility, "selfDeclaredMadeForKids": False}
    if schedule:
        status = {**status, "privacyStatus": "private", "publishAt": schedule}
    token = access_token(http, env)
    size = video.stat().st_size
    started = http.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Upload-Content-Type": "video/*",
            "X-Upload-Content-Length": str(size),
        },
        json={"snippet": {"title": title, "description": description}, "status": status},
    )
    if started.status_code != 200 or "location" not in started.headers:
        raise _error(started, "starting the YouTube upload")
    # ponytail: one PUT of the whole file; resume an interrupted upload by range when videos
    # get big enough for a dropped connection to matter.
    with video.open("rb") as body:
        sent = http.put(
            started.headers["location"],
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "video/*",
                "Content-Length": str(size),
            },
        )
    if sent.status_code not in (200, 201):
        raise _error(sent, "uploading the video")
    uploaded = sent.json()
    return {
        "video_id": uploaded["id"],
        "url": f"https://youtu.be/{uploaded['id']}",
        "publish_at": schedule or None,
    }


@mcp.tool(
    description="Upload a video to YouTube. With `schedule` (ISO 8601) it stays private until"
    " then and goes public at that time."
)
def youtube(
    input_path: str,
    title: str,
    description: str = "",
    visibility: str = "private",
    schedule: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(60.0, write=None)) as http:
        return publish(
            http,
            os.environ,
            Path(input_path),
            title,
            description,
            visibility,
            schedule,
        )


def consent_url(client_id: str, redirect: str, state: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def authorize() -> None:  # pragma: no cover - needs a browser and a Google account
    """The one-time loopback consent that prints YOUTUBE_REFRESH_TOKEN for the .env file."""
    client_id, client_secret = os.environ.get(CREDENTIALS[0]), os.environ.get(CREDENTIALS[1])
    if not client_id or not client_secret:
        sys.exit("export YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first")
    state, got = secrets.token_urlsafe(16), {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state") == [state]:
                got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab.")

    server = HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = consent_url(client_id, redirect, state)
    print(f"Open this link and allow the upload scope:\n{url}")
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    if "error" in got:
        sys.exit(f"consent refused: {got['error']}")
    answer = httpx.post(
        TOKEN_URL,
        data={
            "code": got["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        },
    )
    answer.raise_for_status()
    print(f"YOUTUBE_REFRESH_TOKEN={answer.json()['refresh_token']}")


if __name__ == "__main__":  # pragma: no cover - started as a subprocess
    if sys.argv[1:2] == ["authorize"]:
        authorize()
    else:
        mcp.run("stdio")
