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
from swarmify_connectors.youtube import consent_url, publish

ENV = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=u1"


class FakeGoogle:
    def __init__(self, upload_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.upload_status = upload_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
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


def test_google_errors_and_missing_inputs_are_tool_errors(video: Path, tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(FakeGoogle(403))) as http:
        with pytest.raises(ToolError, match=r"uploading the video failed \(403\): quotaExceeded"):
            publish(http, ENV, video, "t")
        with pytest.raises(ToolError, match="does not exist"):
            publish(http, ENV, tmp_path / "nope.mp4", "t")
        with pytest.raises(ToolError, match="needs YOUTUBE_REFRESH_TOKEN"):
            publish(http, {**ENV, "YOUTUBE_REFRESH_TOKEN": ""}, video, "t")


def test_the_consent_link_asks_for_upload_only_and_offline_access() -> None:
    url = urllib.parse.urlparse(consent_url("id", "http://127.0.0.1:5", "st"))
    query = urllib.parse.parse_qs(url.query)
    assert query["scope"] == ["https://www.googleapis.com/auth/youtube.upload"]
    assert (query["access_type"], query["prompt"], query["state"]) == (
        ["offline"],
        ["consent"],
        ["st"],
    )


def test_the_builtin_server_starts_and_lists_its_one_tool() -> None:
    youtube = next(c for c in BUILTINS if c.name == "youtube")
    assert youtube.command[0] == sys.executable
    (tool,) = McpTransport().list_tools(youtube, {})
    assert tool.name == "youtube"
    assert {"input_path", "title", "visibility", "schedule"} <= set(tool.input_schema["properties"])
