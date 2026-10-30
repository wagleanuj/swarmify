"""The Upload-Post connector against a fake Upload-Post: httpx's MockTransport answers the upload,
status and account requests, so the form, headers and normalized answers are asserted without a
network. Then the package installed through the real install path, given to a world as a default,
started over stdio MCP, and run by the core like any publish connector."""

import shutil
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import upload_post as up
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
    publish,
    read_manifest,
)
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def preview(**params: Any) -> dict[str, Any]:
    base = {"title": "Five hooks", "visibility": "public", "user": "me"}
    return up.preview_of({"inputs": ["short.mp4"], "params": {**base, **params}}, NOW)


def values(shown: dict[str, Any]) -> dict[str, Any]:
    return {f["key"]: f["value"] for f in shown["fields"]}


class FakeUploadPost:
    """Answers each request with the next of `answers`, a (status, JSON body) pair."""

    def __init__(self, *answers: tuple[int, dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        code, body = self.answers.pop(0)
        return httpx.Response(code, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=up.API,
            headers={"Authorization": "Apikey k"},
            transport=httpx.MockTransport(self),
        )


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00video")
    return path


# The preview a gate shows


def test_the_preview_is_what_will_go_out() -> None:
    shown = preview(schedule="2099-01-01T09:00:00Z", platforms=["tiktok", "youtube"])
    assert shown["problems"] == []
    assert values(shown) == {
        "title": "Five hooks",
        "description": "",
        "hashtags": "",
        "tags": "",
        "platforms": ["tiktok", "youtube"],
        "user": "me",
        "visibility": "public",
        "made_for_kids": False,
        "schedule": "2099-01-01T09:00:00Z",
        "video": "short.mp4",
    }
    assert [f["key"] for f in shown["fields"] if f["editable"]] == list(up.EDITABLE)
    assert shown["fields"][8]["hint"] == "Nothing posts before then."
    assert values(preview())["platforms"] == ["youtube"]


def test_metadata_wins_over_params_and_edits_win_over_both() -> None:
    args = {
        "inputs": ["short.mp4"],
        "params": {"title": "Param", "user": "me"},
        "metadata": {"title": "From the script", "hashtags": ["#Owls", "nature"]},
        "edits": {"title": "Edited", "user": "someone else"},  # only editable fields take one
    }
    shown = values(up.preview_of(args, NOW))
    assert (shown["title"], shown["hashtags"], shown["user"]) == ("Edited", "#Owls #nature", "me")


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"user": " "}, "needs params.user"),
        ({"title": "x" * 101}, "the limit is 100"),
        ({"schedule": "+0d 09:00"}, "has already passed"),
        ({"schedule": "09:00 Mars/Olympus"}, "unknown timezone"),
        ({"schedule": "2099-01-01T09:00:00Z", "visibility": "unlisted"}, "visibility: public"),
        ({"hashtags": "#a #b #c #d #e #f"}, "6 hashtags, at most 5"),
        ({"tags": ", ".join(["x" * 49] * 9) + ", y"}, "the tags take 451 characters, at most 450"),
        ({"description": "é" * 2_498, "hashtags": "#ok"}, "is 5001 bytes, the limit is 5000"),
    ],
)
def test_the_preview_names_every_problem(params: dict[str, Any], problem: str) -> None:
    problems = preview(**params)["problems"]
    assert any(problem in p for p in problems), problems


# What goes out


def test_the_form_carries_the_confirmed_values_in_upload_posts_names() -> None:
    sent = values(preview(description="Why owls turn.", hashtags="#owls", tags="owls, owl facts"))
    assert up.form({**sent, "made_for_kids": True}) == {
        "user": "me",
        "title": "Five hooks",
        "platform[]": ["youtube"],
        "async_upload": "false",
        "description": "Why owls turn.\n\n#owls",
        "tags[]": ["owls", "owl facts"],
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": "true",
    }
    tiktok = up.form({**values(preview(platforms=["tiktok"])), "schedule": "2099-01-01T09:00:00Z"})
    assert tiktok == {
        "user": "me",
        "title": "Five hooks",
        "platform[]": ["tiktok"],
        "async_upload": "false",
        "scheduled_date": "2099-01-01T09:00:00Z",
    }  # YouTube's fields only when posting there


def test_a_scheduled_post_is_sent_with_its_key_and_records_the_job_id(video: Path) -> None:
    fake = FakeUploadPost((200, {"success": True, "job_id": "job_9"}))
    sent = values(preview(schedule="2099-01-01T09:00:00Z", platforms=["tiktok", "youtube"]))
    with fake.client() as http:
        out = up.publish_with(http, sent, "stage_1", str(video))
    assert out == {"status": "scheduled", "links": {}, "id": "job_9", "failed": {}}
    (request,) = fake.requests
    assert (request.method, str(request.url)) == ("POST", f"{up.API}/upload")
    assert request.headers["authorization"] == "Apikey k"
    assert request.headers["idempotency-key"] == "stage_1"
    body = request.content
    for name, value in [
        ("user", b"me"),
        ("platform[]", b"tiktok"),
        ("platform[]", b"youtube"),
        ("scheduled_date", b"2099-01-01T09:00:00Z"),
        ("async_upload", b"false"),
    ]:
        assert f'name="{name}"\r\n\r\n'.encode() + value in body, name
    assert b'filename="short.mp4"' in body and b"\x00video" in body
    md = publish.markdown("upload_post", preview()["fields"], publish.result("upload_post", out))
    assert "- no link yet: upload_post job job_9 is scheduled" in md


def test_a_post_now_records_each_platform_link_and_failure(video: Path) -> None:
    results = {
        "tiktok": {"success": True, "url": "https://tiktok.com/@me/video/1"},
        "instagram": {"success": False, "error": "Expired access token"},
    }
    fake = FakeUploadPost((200, {"success": True, "results": results}))
    with fake.client() as http:
        out = up.publish_with(http, values(preview()), "stage_1", str(video))
    assert out == {
        "status": "published",
        "links": {"tiktok": "https://tiktok.com/@me/video/1"},
        "id": None,
        "failed": {"instagram": "Expired access token"},
    }


def test_an_upload_moved_to_the_background_becomes_a_long_job_polled_to_its_links(
    video: Path,
) -> None:
    done = [{"platform": "youtube", "success": True, "post_url": "https://youtu.be/abc123"}]
    fake = FakeUploadPost(
        (200, {"success": True, "message": "processing", "request_id": "req_9"}),
        (200, {"success": True, "status": "in_progress"}),
        (200, {"success": True, "status": "completed", "results": done}),
    )
    with fake.client() as http:
        job = up.publish_with(http, values(preview()), "stage_1", str(video))
        assert (job["job_id"], job["poll"], job["status"]) == ("req_9", "upload_status", "running")
        assert up.status_with(http, "req_9")["status"] == "running"
        out = up.status_with(http, "req_9")
    assert out == {
        "status": "done",
        "links": {"youtube": "https://youtu.be/abc123"},
        "id": None,
        "failed": {},
    }
    assert str(fake.requests[1].url) == f"{up.API}/uploadposts/status?request_id=req_9"


@pytest.mark.parametrize(
    ("answer", "failed"),
    [
        (
            {
                "success": True,
                "status": "completed",
                "results": [{"platform": "youtube", "success": False, "message": "quota"}],
            },
            {"youtube": "quota"},
        ),
        (
            {"success": False, "status": "failed", "message": "file unreadable"},
            {"upload_post": "file unreadable"},
        ),
        ({"success": False, "error": "Request not found"}, {"upload_post": "Request not found"}),
    ],
)
def test_a_background_upload_that_reached_no_platform_says_why(
    answer: dict[str, Any], failed: dict[str, str]
) -> None:
    with FakeUploadPost((200, answer)).client() as http:
        out = up.status_with(http, "req_9")
    assert (out["status"], out["links"], out["failed"]) == ("done", {}, failed)


def test_upload_post_errors_and_missing_videos_are_tool_errors(video: Path) -> None:
    fake = FakeUploadPost((401, {"message": "Invalid API key"}))
    with fake.client() as http, pytest.raises(ToolError, match="401: Invalid API key"):
        up.publish_with(http, values(preview()), "stage_1", str(video))
    with fake.client() as http, pytest.raises(ToolError, match="is no file"):
        up.publish_with(http, values(preview()), "stage_1", str(video.parent / "gone.mp4"))


def test_the_key_check_names_the_account_or_says_why_not(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeUploadPost((200, {"success": True, "email": "me@example.com"}), (401, {}))
    with fake.client() as http:
        assert up.key_check_with(http) == {
            "ok": True,
            "message": "Signed in to Upload-Post as me@example.com.",
        }
        bad = up.key_check_with(http)
    assert bad["ok"] is False and "401" in bad["message"]
    assert str(fake.requests[0].url) == f"{up.API}/uploadposts/me"
    monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
    with pytest.raises(ToolError, match="needs its API key"):
        up.check_key()


# Installed from this folder as a default package, as Swarmify does at startup.


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0)
    connectors.packages.repo = data / "repo"
    return connectors


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real install for the module, from a repository holding only this package."""
    data = tmp_path_factory.mktemp("data")
    skip = shutil.ignore_patterns(".venv", "__pycache__")
    shutil.copytree(HERE, data / "repo" / "connectors" / "upload_post", ignore=skip)
    assert registry(data, FakeTransport()).install_defaults() == {"upload_post": "1.0.0"}
    return data / "packages"


@pytest.fixture
def given(installed: Path, tmp_path: Path) -> Callable[[Any], Connectors]:
    """A registry sharing that install, the package given to world `w` as a default."""

    def make(transport: Any) -> Connectors:
        (tmp_path / "packages").symlink_to(installed)
        shutil.copytree(installed.parent / "repo", tmp_path / "repo")
        connectors = registry(tmp_path, transport)
        connectors.install_defaults()  # installed already: only remembered
        connectors.offer_defaults("w")
        return connectors

    return make


def test_the_manifest_passes_and_its_key_reaches_the_process() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["setup"]["test"]) == ("upload_post", "publish", "check_key")
    (key,) = m["config"]
    assert (key["env"], key["secret"], key["required"]) == ("UPLOAD_POST_API_KEY", True, True)


def test_a_stage_is_checked_for_its_profile_and_video_before_a_run(
    given: Callable[[Any], Connectors],
) -> None:
    connectors = given(FakeTransport())
    assert connectors.stage_problems("w", {"calls": "upload_post", "params": {}}) == [
        "upload_post needs an input, the video",
        "upload_post needs params.user, the Upload-Post profile",
    ]
    ok = {"calls": "upload_post", "input": "short.mp4", "params": {"user": "me"}}
    assert connectors.stage_problems("w", ok) == []


def test_the_core_polls_a_background_upload_like_any_long_job(
    given: Callable[[Any], Connectors],
) -> None:
    transport = FakeTransport()
    connectors = given(transport)
    transport.outputs["check_key"] = [{"ok": False, "message": "401"}, {"ok": True}]
    assert connectors.save_setup("w", "upload_post", {"api_key": "bad"}, {}).ok is False  # type: ignore[union-attr]
    assert connectors.missing(connectors.find("w", "upload_post")) == ["api_key"]  # type: ignore[arg-type]
    connectors.save_setup("w", "upload_post", {"api_key": "k"}, {})
    assert [c[1:] for c in transport.calls] == [
        ("check_key", {}, {"UPLOAD_POST_API_KEY": "bad"}),
        ("check_key", {}, {"UPLOAD_POST_API_KEY": "k"}),
    ]  # its Setup test: the key is saved only once it passes
    transport.calls.clear()
    transport.outputs["publish"] = [{"job_id": "req_9", "poll": "upload_status"}]
    links = {"youtube": "https://youtu.be/abc123"}
    transport.outputs["upload_status"] = [
        {"status": "running"},
        {"status": "done", "links": links, "id": None, "failed": {}},
    ]
    sent = values(preview())
    config = {
        "calls": "upload_post",
        "input": "short.mp4",
        "params": {"user": "me", "input_path": "/runs/r/short.mp4"},
        "confirmed": sent,
    }
    call = connectors.run_stage("w", "run_1", "stage_1", config)
    publish_call, *polls = transport.calls
    assert publish_call == (
        "upload_post",
        "publish",
        {"fields": sent, "idempotency_key": "stage_1", "input_path": "/runs/r/short.mp4"},
        {"UPLOAD_POST_API_KEY": "k"},
    )
    assert [p[1:3] for p in polls] == [("upload_status", {"job_id": "req_9"})] * 2
    assert connectors.publish_result(call)["links"] == links

    transport.outputs["publish"] = [{"links": {}, "failed": {"youtube": "quota"}}]
    with pytest.raises(ConnectorError, match="nothing was posted. youtube: quota"):
        connectors.run_stage("w", "run_1", "stage_2", config)


def test_the_installed_package_starts_over_stdio_with_its_tools(
    given: Callable[[Any], Connectors],
) -> None:
    connectors = given(McpTransport())
    found = connectors.find("w", "upload_post")
    assert found is not None and found.version == "1.0.0"
    connectors.setups.put_value("upload_post", None, "api_key", "k")  # no live key check
    tools = connectors.transport.list_tools(found, {"UPLOAD_POST_API_KEY": "k"})
    assert sorted(t.name for t in tools) == ["check_key", "preview", "publish", "upload_status"]
    shown = connectors.publish_preview("w", {"calls": "upload_post", "params": {}})
    assert shown is not None and shown["connector"] == "upload_post"
    assert "upload_post needs params.user, the Upload-Post profile to post as" in shown["problems"]
