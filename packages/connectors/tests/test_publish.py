"""Publish stages at the Connectors seam: the preview a gate shows, the call that sends exactly
that, the normalized result, and the refusals. Real SQLite, the recording fake transport."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import Call, ConnectorError, Connectors, FakeTransport, publish

W = "world_1"
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
YOUTUBE_KEYS = {
    "YOUTUBE_CLIENT_ID": "id",
    "YOUTUBE_CLIENT_SECRET": "secret",
    "YOUTUBE_REFRESH_TOKEN": "refresh",
}


def stage(calls: str = "youtube", **params: Any) -> dict[str, Any]:
    base = {"title": "Five hooks", "visibility": "public", "input_path": "/runs/r/short.mp4"}
    return {"calls": calls, "input": "short.mp4", "params": {**base, **params}}


def test_the_preview_is_what_will_go_out() -> None:
    shown = publish.preview(
        "youtube", stage(schedule="2026-11-03T10:00:00+01:00", description="d"), NOW
    )
    assert shown == {
        "connector": "youtube",
        "title": "Five hooks",
        "description": "d",
        "visibility": "public",
        "schedule": "2026-11-03T09:00:00Z",
        "video": "short.mp4",
        "platforms": [],
        "made_for_kids": False,
        "problems": [],
    }
    assert publish.preview("upload_post", stage("upload_post", user="me"), NOW)["platforms"] == [
        "youtube"
    ]


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (stage(title=" "), "needs params.title"),
        (stage(title="x" * 101), "the limit is 100"),
        (stage(visibility="secret"), "visibility is one of"),
        (stage(schedule="next tuesday"), "not an ISO 8601 time"),
        (stage(schedule="+1d 25:00"), "not an ISO 8601 time"),
        (stage(schedule="09:00 Mars/Olympus"), "unknown timezone 'Mars/Olympus'"),
        (stage(schedule="+0d 09:00"), "has already passed"),
        (stage(made_for_kids="no"), "made_for_kids is true or false"),
        (stage(schedule="2026-11-03T09:00:00"), "needs a timezone"),
        (stage(schedule="2026-11-03T09:00:00Z", visibility="unlisted"), "set visibility: public"),
        (stage(schedule="2026-10-01T09:00:00Z"), "has already passed"),
        ({**stage(), "input": None}, "needs an input"),
        (stage("upload_post"), "needs params.user"),
    ],
)
def test_the_preview_names_every_problem(config: dict[str, Any], problem: str) -> None:
    connector = config["calls"]
    assert any(problem in p for p in publish.preview(connector, config, NOW)["problems"])


@pytest.mark.parametrize(
    ("relative", "resolved"),
    [
        ("13:30", "2026-10-26T13:30:00Z"),  # later today
        ("09:00", "2026-10-27T09:00:00Z"),  # already past today, so tomorrow
        ("+1d 09:00", "2026-10-27T09:00:00Z"),
        ("+2d 09:00 America/New_York", "2026-10-28T13:00:00Z"),
    ],
)
def test_a_relative_schedule_resolves_against_now(relative: str, resolved: str) -> None:
    shown = publish.preview("youtube", stage(schedule=relative), NOW)
    assert (shown["schedule"], shown["problems"]) == (resolved, [])


def test_made_for_kids_reaches_youtube_and_upload_post() -> None:
    assert publish.preview("youtube", stage(made_for_kids=True), NOW)["made_for_kids"] is True
    _, args = publish.request("youtube", stage(made_for_kids=True), "k")  # type: ignore[misc]
    assert args["made_for_kids"] is True
    config = stage("upload_post", user="me", made_for_kids=True)
    _, args = publish.request("upload_post", config, "k")  # type: ignore[misc]
    assert args["platformOptions"]["youtubeSelfDeclaredMadeForKids"] is True


def test_youtube_uploads_with_the_previewed_values(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set(YOUTUBE_KEYS)
    transport.outputs["youtube"] = [
        {"video_id": "abc", "url": "https://youtu.be/abc", "publish_at": "2099-01-01T09:00:00Z"}
    ]
    call = connectors.run_stage(W, "run_1", "stage_1", stage(schedule="2099-01-01T09:00:00Z"))
    ((name, tool, args, env),) = transport.calls
    assert (name, tool, env) == ("youtube", "youtube", YOUTUBE_KEYS)
    assert args == {
        "idempotency_key": "stage_1",
        "input_path": "/runs/r/short.mp4",
        "title": "Five hooks",
        "description": "",
        "visibility": "public",
        "schedule": "2099-01-01T09:00:00Z",
        "made_for_kids": False,
    }
    assert call.status == "done"
    assert connectors.publish_result(call) == {
        "status": "scheduled",
        "links": {"youtube": "https://youtu.be/abc"},
        "id": "abc",
        "failed": {},
    }


def test_upload_post_gets_its_own_argument_names_and_an_idempotency_key(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    transport.outputs["upload_video"] = [
        {"result": {"success": True, "job_id": "job_9", "scheduled_date": "2099-01-01T09:00:00Z"}}
    ]
    config = stage(
        "upload_post", user="me", platforms=["tiktok", "youtube"], schedule="2099-01-01T09:00:00Z"
    )
    call = connectors.run_stage(W, "run_1", "stage_1", config)
    ((_, tool, args, env),) = transport.calls
    assert (tool, env) == ("upload_video", {"UPLOAD_POST_API_KEY": "k"})
    assert args == {
        "videoPathOrUrl": "/runs/r/short.mp4",
        "user": "me",
        "platforms": ["tiktok", "youtube"],
        "title": "Five hooks",
        "description": "",
        "asyncUpload": False,
        "platformOptions": {
            "idempotencyKey": "stage_1",
            "youtubePrivacyStatus": "public",
            "youtubeSelfDeclaredMadeForKids": False,
        },
        "scheduledDate": "2099-01-01T09:00:00Z",
    }
    assert connectors.publish_result(call) == {
        "status": "scheduled",
        "links": {},
        "id": "job_9",
        "failed": {},
    }


def test_upload_post_now_records_each_platform_link_and_failure(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    results = {
        "tiktok": {"success": True, "url": "https://tiktok.com/@me/video/1"},
        "instagram": {"success": False, "error": "Expired access token"},
    }
    transport.outputs["upload_video"] = [{"result": {"success": True, "results": results}}]
    call = connectors.run_stage(W, "run_1", "stage_1", stage("upload_post", user="me"))
    out = connectors.publish_result(call)
    assert out["links"] == {"tiktok": "https://tiktok.com/@me/video/1"}
    assert out["failed"] == {"instagram": "Expired access token"}
    md = publish.markdown(publish.preview("upload_post", stage("upload_post", user="me")), out)
    assert "- tiktok: https://tiktok.com/@me/video/1" in md
    assert "- instagram failed: Expired access token" in md


def test_a_post_that_reached_no_platform_fails_the_stage(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    failed = {"youtube": {"success": False, "error": "quota"}}
    transport.outputs["upload_video"] = [{"result": {"success": False, "results": failed}}]
    with pytest.raises(ConnectorError, match="nothing was posted. youtube: quota"):
        connectors.run_stage(W, "run_1", "stage_1", stage("upload_post", user="me"))
    assert connectors.call_for("stage_1").status == "failed"  # type: ignore[union-attr]


def test_an_upload_post_moved_to_the_background_fails_with_its_request_id(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    background = {"success": True, "message": "processing", "request_id": "req_9"}
    transport.outputs["upload_video"] = [{"result": {**background, "total_platforms": 1}}]
    with pytest.raises(ConnectorError, match="status for request req_9"):
        connectors.run_stage(W, "run_1", "stage_1", stage("upload_post", user="me"))
    call = connectors.call_for("stage_1")
    assert call is not None and call.status == "failed"
    assert connectors.publish_result(call)["id"] == "req_9"


def test_a_publish_stage_with_a_problem_never_calls(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set(YOUTUBE_KEYS)
    with pytest.raises(ConnectorError, match="needs params.title"):
        connectors.run_stage(W, "run_1", "stage_1", stage(title=""))
    assert transport.calls == []


def test_an_interrupted_publish_call_is_never_repeated(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set(YOUTUBE_KEYS)
    now = NOW.isoformat()
    connectors.storage.save_call(
        Call("stage_1", W, "run_1", "youtube", "youtube", {}, "running", now, now, attempts=1)
    )
    with pytest.raises(ConnectorError, match="interrupted and may have posted"):
        connectors.run_stage(W, "run_1", "stage_1", stage())
    assert transport.calls == []


def test_publish_previews_only_for_publish_connectors(connectors: Connectors) -> None:
    assert connectors.publish_preview(W, stage())["title"] == "Five hooks"  # type: ignore[index]
    assert connectors.publish_preview(W, {"calls": "remotion_render"}) is None
    assert connectors.publish_preview(W, {"calls": "nope"}) is None


def test_builtin_publish_connectors_name_their_keys_and_take_only_those(
    connectors: Connectors, tmp_path: Path
) -> None:
    youtube = connectors.find(W, "youtube")
    assert youtube is not None and youtube.has_server
    assert connectors.missing(youtube) == list(YOUTUBE_KEYS)
    connectors.set_credentials(youtube, {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": ""})
    assert connectors.missing(youtube) == ["YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
    with pytest.raises(ValueError, match="does not use FAL_KEY"):
        connectors.set_credentials(youtube, {"FAL_KEY": "k"})
    assert "FAL_KEY" not in (tmp_path / ".env").read_text()


@pytest.mark.parametrize(
    ("fields", "ids"),
    [
        (
            {
                "connector": "youtube",
                "id": "abc123",
                "links": {"youtube": "https://youtu.be/abc123"},
            },
            ["abc123"],
        ),
        ({"connector": "youtube", "id": "sched1", "links": {}}, ["sched1"]),
        (
            {
                "connector": "upload_post",
                "id": None,
                "links": {
                    "youtube": "https://www.youtube.com/shorts/short_1",
                    "tiktok": "https://www.tiktok.com/@me/video/1",
                    "other": "https://youtube.com/watch?v=watch-2&t=3",
                },
            },
            ["short_1", "watch-2"],
        ),
        ({"connector": "upload_post", "id": "job_9", "links": {}}, []),
        ({"links": "not a map"}, []),
    ],
)
def test_youtube_ids_are_read_from_links_and_youtube_posts(
    fields: dict[str, Any], ids: list[str]
) -> None:
    assert publish.youtube_ids(fields) == ids


def test_view_counts_come_from_the_youtube_connector_with_its_keys(
    connectors: Connectors, transport: FakeTransport
) -> None:
    with pytest.raises(ConnectorError, match="youtube needs YOUTUBE_CLIENT_ID"):
        connectors.youtube_views(W, ["v1"])
    youtube = connectors.find(W, "youtube")
    assert youtube is not None
    connectors.set_credentials(youtube, YOUTUBE_KEYS)
    transport.outputs["views"] = [{"views": {"v1": 41000}}, {"nothing": True}]
    assert connectors.youtube_views(W, ["v1", "v2"]) == {"v1": 41000}
    assert transport.calls[-1] == ("youtube", "views", {"video_ids": ["v1", "v2"]}, YOUTUBE_KEYS)
    with pytest.raises(ConnectorError, match="without counts"):
        connectors.youtube_views(W, ["v1"])
    assert connectors.youtube_views(W, []) == {}
