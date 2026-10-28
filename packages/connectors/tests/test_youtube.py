"""The YouTube wrapper against a fake Google: httpx's MockTransport answers the token, upload
start and upload requests, so the Data API calls are asserted without a network."""

import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import BUILTINS, McpTransport
from swarmify_connectors.youtube import consent_url, publish, video_views

ENV = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=u1"


class FakeGoogle:
    """`uploads` are the channel's videos, newest first, as `videos.list` part snippet answers."""

    def __init__(self, upload_status: int = 200, uploads: list[dict] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.upload_status = upload_status
        self.uploads = uploads or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/channels"):
            playlists = {"relatedPlaylists": {"uploads": "UU1"}}
            return httpx.Response(200, json={"items": [{"contentDetails": playlists}]})
        if request.url.path.endswith("/playlistItems"):
            items = [{"contentDetails": {"videoId": v["id"]}} for v in self.uploads]
            return httpx.Response(200, json={"items": items})
        if request.url.path == "/youtube/v3/videos":
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [v for v in self.uploads if v["id"] in ids]})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": SESSION})
        if self.upload_status != 200:
            return httpx.Response(self.upload_status, json={"error": {"message": "quotaExceeded"}})
        meta = {"id": "vid1", "status": {"privacyStatus": "private"}}
        return httpx.Response(200, json=meta)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00video")
    return path


def test_a_scheduled_upload_is_private_with_publish_at(video: Path) -> None:
    google = FakeGoogle()
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        out = publish(http, ENV, video, "Five hooks", "d", "public", "2099-01-01T09:00:00Z")
    assert out == {
        "video_id": "vid1",
        "url": "https://youtu.be/vid1",
        "publish_at": "2099-01-01T09:00:00Z",
    }
    token, start, upload = google.requests
    assert urllib.parse.parse_qs(token.content.decode())["refresh_token"] == ["r"]
    assert start.url.params["uploadType"] == "resumable"
    assert start.url.params["part"] == "snippet,status"
    assert start.headers["authorization"] == "Bearer tok"
    assert start.headers["x-upload-content-length"] == str(len(b"\x00video"))
    assert json.loads(start.content) == {
        "snippet": {"title": "Five hooks", "description": "d"},
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "publishAt": "2099-01-01T09:00:00Z",
        },
    }
    assert str(upload.url) == SESSION and upload.method == "PUT"
    assert upload.read() == b"\x00video"


def test_an_unscheduled_upload_takes_its_visibility_now(video: Path) -> None:
    google = FakeGoogle()
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        out = publish(http, ENV, video, "t", visibility="unlisted")
    assert out["publish_at"] is None
    status = json.loads(google.requests[1].content)["status"]
    assert status == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}


def test_made_for_kids_is_declared_to_youtube(video: Path) -> None:
    google = FakeGoogle()
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        publish(http, ENV, video, "t", made_for_kids=True)
    assert json.loads(google.requests[1].content)["status"]["selfDeclaredMadeForKids"] is True


def test_a_retry_finds_the_video_already_uploaded_under_its_key(video: Path) -> None:
    earlier = {"id": "vid0", "snippet": {"tags": ["swarmify-stage_1"]}}
    other = {"id": "vidx", "snippet": {"title": "no tags"}}
    google = FakeGoogle(uploads=[other, earlier])
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        out = publish(http, ENV, video, "t", idempotency_key="stage_1")
    assert out == {"video_id": "vid0", "url": "https://youtu.be/vid0", "publish_at": None}
    assert [r.method for r in google.requests] == ["POST", "GET", "GET", "GET"]
    _, channels, items, videos = google.requests
    assert channels.url.params["mine"] == "true"
    assert items.url.params["playlistId"] == "UU1"
    assert videos.url.params["id"] == "vidx,vid0" and videos.url.params["part"] == "snippet"


def test_a_first_upload_is_tagged_with_its_key(video: Path) -> None:
    google = FakeGoogle(uploads=[{"id": "vidx", "snippet": {"tags": ["swarmify-stage_2"]}}])
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        out = publish(http, ENV, video, "t", idempotency_key="stage_1")
    assert out["video_id"] == "vid1"
    start = next(r for r in google.requests if r.url.path.startswith("/upload"))
    assert json.loads(start.content)["snippet"]["tags"] == ["swarmify-stage_1"]


def test_google_errors_and_missing_inputs_are_tool_errors(video: Path, tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(FakeGoogle(403))) as http:
        with pytest.raises(ToolError, match=r"uploading the video failed \(403\): quotaExceeded"):
            publish(http, ENV, video, "t")
        with pytest.raises(ToolError, match="does not exist"):
            publish(http, ENV, tmp_path / "nope.mp4", "t")
        with pytest.raises(ToolError, match="needs YOUTUBE_REFRESH_TOKEN"):
            publish(http, {**ENV, "YOUTUBE_REFRESH_TOKEN": ""}, video, "t")


def test_the_consent_link_asks_for_upload_and_read_only_and_offline_access() -> None:
    url = urllib.parse.urlparse(consent_url("id", "http://127.0.0.1:5", "st"))
    query = urllib.parse.parse_qs(url.query)
    assert query["scope"][0].split() == [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]
    assert (query["access_type"], query["prompt"], query["state"]) == (
        ["offline"],
        ["consent"],
        ["st"],
    )


def test_view_counts_come_from_videos_list_in_batches_of_50() -> None:
    seen: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        ids = request.url.params["id"].split(",")
        items = [{"id": i, "statistics": {"viewCount": str(len(i))}} for i in ids if i != "gone"]
        return httpx.Response(200, json={"items": items})

    ids = [f"v{n}" for n in range(60)] + ["gone", "v1"]
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        counts = video_views(http, ENV, ids)
        assert video_views(http, ENV, []) == {}
    assert counts["v1"] == 2 and counts["v59"] == 3 and "gone" not in counts and len(counts) == 60
    _, first, second = seen
    assert first.url.path == "/youtube/v3/videos" and first.url.params["part"] == "statistics"
    assert len(first.url.params["id"].split(",")) == 50
    assert second.url.params["id"].split(",") == [*(f"v{n}" for n in range(50, 60)), "gone"]
    assert first.headers["authorization"] == "Bearer tok"

    def refused(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(403, json={"error": {"message": "insufficientPermissions"}})

    with httpx.Client(transport=httpx.MockTransport(refused)) as http:
        with pytest.raises(ToolError, match=r"view counts failed \(403\): insufficient"):
            video_views(http, ENV, ["v1"])


def test_the_builtin_server_starts_and_lists_its_two_tools() -> None:
    youtube = next(c for c in BUILTINS if c.name == "youtube")
    assert youtube.command[0] == sys.executable
    tool, views = McpTransport().list_tools(youtube, {})
    assert (tool.name, views.name) == ("youtube", "views")
    assert set(views.input_schema["properties"]) == {"video_ids"}
    assert {"input_path", "title", "visibility", "schedule"} <= set(tool.input_schema["properties"])
