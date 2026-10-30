"""Publish stages at the Connectors seam: the preview a gate shows, the call that sends exactly
what was confirmed, the normalized result, and the refusals, for the built-ins and for a world's
own text-publish connector. Real SQLite, the recording fake transport."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import (
    Call,
    ConnectorError,
    Connectors,
    FakeTransport,
    builtin_publish,
    publish,
)

W = "world_1"
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
YOUTUBE_KEYS = {
    "YOUTUBE_CLIENT_ID": "id",
    "YOUTUBE_CLIENT_SECRET": "secret",
    "YOUTUBE_REFRESH_TOKEN": "refresh",
}


def preview(
    connector: str,
    config: dict[str, Any],
    now: datetime = NOW,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A built-in's preview of the stage, as the core asks for it."""
    inputs = [config["input"]] if config.get("input") else []
    args = {"inputs": inputs, "params": config["params"], "metadata": metadata, "edits": edits}
    return builtin_publish.preview(connector, args, now)


def values(shown: dict[str, Any]) -> dict[str, Any]:
    return {f["key"]: f["value"] for f in shown["fields"]}


def stage(calls: str = "youtube", **params: Any) -> dict[str, Any]:
    """A publish stage's config as it reaches `run_stage`: with the values its gate confirmed."""
    base = {"title": "Five hooks", "visibility": "public", "input_path": "/runs/r/short.mp4"}
    config = {"calls": calls, "input": "short.mp4", "params": {**base, **params}}
    return {**config, "confirmed": values(preview(calls, config))}


def test_the_preview_is_what_will_go_out() -> None:
    shown = preview("youtube", stage(schedule="2026-11-03T10:00:00+01:00", description="d"))
    assert shown["problems"] == []
    assert [(f["key"], f["label"], f["editable"]) for f in shown["fields"]] == [
        ("title", "Title", True),
        ("description", "Description", True),
        ("visibility", "Visibility", False),
        ("made_for_kids", "Made for kids", False),
        ("schedule", "When", False),
        ("video", "Video", False),
    ]
    assert values(shown) == {
        "title": "Five hooks",
        "description": "d",
        "visibility": "public",
        "made_for_kids": False,
        "schedule": "2026-11-03T09:00:00Z",
        "video": "short.mp4",
    }
    assert shown["fields"][4]["hint"] == "Private until then, then public."
    assert values(preview("upload_post", stage("upload_post", user="me")))["platforms"] == [
        "youtube"
    ]


def test_metadata_wins_over_params_and_edits_win_over_both() -> None:
    metadata = {"title": "From the script", "description": "Its words", "type": "script"}
    shown = values(preview("youtube", stage(), metadata=metadata))
    assert (shown["title"], shown["description"]) == ("From the script", "Its words")
    edits = {"title": "Edited", "visibility": "private"}  # only editable fields take an edit
    shown = values(preview("youtube", stage(), metadata=metadata, edits=edits))
    assert (shown["title"], shown["visibility"]) == ("Edited", "public")


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
    assert any(problem in p for p in preview(config["calls"], config)["problems"])


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
    shown = preview("youtube", stage(schedule=relative))
    assert (values(shown)["schedule"], shown["problems"]) == (resolved, [])


def test_made_for_kids_reaches_youtube_and_upload_post() -> None:
    config = stage(made_for_kids=True)
    assert config["confirmed"]["made_for_kids"] is True
    _, args = builtin_publish.request("youtube", config["confirmed"], config["params"], "k")
    assert args["made_for_kids"] is True
    config = stage("upload_post", user="me", made_for_kids=True)
    _, args = builtin_publish.request("upload_post", config["confirmed"], config["params"], "k")
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
    shown = preview("upload_post", stage("upload_post", user="me"))
    md = publish.markdown("upload_post", shown["fields"], out)
    assert md.startswith("# Five hooks\n\nSent to upload_post:\n\n- Title: Five hooks\n")
    assert "- When: Right after confirming.\n" in md
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


def test_an_upload_post_moved_to_the_background_is_polled_until_its_links_are_known(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    background = {"success": True, "message": "processing", "request_id": "req_9"}
    transport.outputs["upload_video"] = [{"result": {**background, "total_platforms": 1}}]
    done = [{"platform": "youtube", "success": True, "post_url": "https://youtu.be/abc123"}]
    transport.outputs["get_status"] = [
        {"result": {"success": True, "status": "in_progress"}},
        {"result": {"success": True, "status": "completed", "results": done}},
    ]
    call = connectors.run_stage(W, "run_1", "stage_1", stage("upload_post", user="me"))
    assert call.status == "done" and call.polls == 2
    assert [c[1:3] for c in transport.calls[1:]] == [("get_status", {"requestId": "req_9"})] * 2
    out = connectors.publish_result(call)
    assert out["status"] == "published"
    assert out["links"] == {"youtube": "https://youtu.be/abc123"}


def test_a_background_upload_that_reached_no_platform_fails_the_stage(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"UPLOAD_POST_API_KEY": "k"})
    transport.outputs["upload_video"] = [{"result": {"success": True, "request_id": "req_9"}}]
    failed = [{"platform": "youtube", "success": False, "message": "quota"}]
    transport.outputs["get_status"] = [
        {"result": {"success": True, "status": "completed", "results": failed}}
    ]
    with pytest.raises(ConnectorError, match="nothing was posted. youtube: quota"):
        connectors.run_stage(W, "run_1", "stage_1", stage("upload_post", user="me"))
    assert connectors.call_for("stage_1").status == "failed"  # type: ignore[union-attr]
    gone = {"result": {"success": False, "status": "failed", "message": "file unreadable"}}
    transport.outputs["get_status"] = [gone]
    with pytest.raises(ConnectorError, match="nothing was posted. upload_post: file unreadable"):
        connectors.run_stage(W, "run_1", "stage_2", stage("upload_post", user="me"))
    unknown = {"result": {"success": False, "error": "Request not found"}}
    transport.outputs["get_status"] = [unknown]
    with pytest.raises(ConnectorError, match="nothing was posted. upload_post: Request not found"):
        connectors.run_stage(W, "run_1", "stage_3", stage("upload_post", user="me"))


def test_a_publish_stage_no_gate_confirmed_never_calls(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set(YOUTUBE_KEYS)
    unconfirmed = {k: v for k, v in stage().items() if k != "confirmed"}
    with pytest.raises(ConnectorError, match="publishes only the values a gate confirmed"):
        connectors.run_stage(W, "run_1", "stage_1", unconfirmed)
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


def test_an_interrupted_publish_is_never_repeated_within_its_visit(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set(YOUTUBE_KEYS)
    now = NOW.isoformat()
    connectors.storage.save_call(
        Call(
            "stage_1",
            W,
            "run_1",
            "youtube",
            "youtube",
            {},
            "running",
            now,
            now,
            attempts=1,
            visit=1,
        )
    )
    with pytest.raises(ConnectorError, match="interrupted and may have posted"):
        connectors.run_stage(W, "run_1", "stage_1", stage(), visit=1)
    assert transport.calls == []
    connectors.run_stage(W, "run_1", "stage_1", stage(), visit=2)  # a send-back reruns it
    assert transport.calls[0][2]["idempotency_key"] == "stage_1.2"


def test_publish_previews_only_for_publish_connectors(connectors: Connectors) -> None:
    shown = connectors.publish_preview(W, stage())
    assert shown is not None and shown["connector"] == "youtube"
    assert values(shown)["title"] == "Five hooks"
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


def test_a_saved_publish_stage_is_checked_like_its_gate_will_be(connectors: Connectors) -> None:
    bare = {"calls": "youtube", "input": "short.mp4", "params": {}}
    assert connectors.stage_problems(W, bare) == ["a publish stage needs params.title"]
    assert connectors.stage_problems(W, {**bare, "input": None}) == [
        "a publish stage needs params.title",
        "a publish stage needs an input: the video to post",
    ]
    # the title may come from the script's frontmatter at run time instead
    assert connectors.stage_problems(W, {**bare, "params": {"metadata": "script.md"}}) == []
    assert connectors.stage_problems(W, stage("upload_post")) == [
        "upload_post needs params.user, the Upload-Post profile to post as"
    ]
    assert connectors.stage_problems(W, stage()) == []
    assert connectors.stage_problems(W, {"calls": "web_search"}) == []
    assert connectors.stage_problems(W, {"calls": "nope"}) == []


def poster(connectors: Connectors) -> None:
    """A world's own text-publish connector: a webhook that posts a message."""
    connectors.add(
        connectors.draft(W, "webhook", "publish", "webhook-mcp", ["HOOK_URL"], ["connector"]),
        {"HOOK_URL": "https://hooks.example/1"},
    )


def test_a_worlds_publish_connector_previews_and_publishes_through_its_own_tools(
    connectors: Connectors, transport: FakeTransport
) -> None:
    poster(connectors)
    fields = [
        {"key": "channel", "label": "Channel", "value": "#news"},
        {"key": "text", "label": "Message", "value": "Hello", "editable": True},
    ]
    transport.outputs["preview"] = [{"fields": fields, "problems": []}]
    config = {"calls": "webhook", "input": "post.md", "params": {"channel": "#news"}}
    shown = connectors.publish_preview(W, config, {"text": "Hello"}, {"text": "Hi"})
    assert shown == {
        "connector": "webhook",
        "fields": [
            {"key": "channel", "label": "Channel", "value": "#news", "editable": False, "hint": ""},
            {"key": "text", "label": "Message", "value": "Hello", "editable": True, "hint": ""},
        ],
        "problems": [],
    }
    ((name, tool, args, env),) = transport.calls
    assert (name, tool, env) == ("webhook", "preview", {"HOOK_URL": "https://hooks.example/1"})
    assert args == {
        "inputs": ["post.md"],
        "params": {"channel": "#news"},
        "metadata": {"text": "Hello"},
        "edits": {"text": "Hi"},
    }
    assert connectors.stage_problems(W, config) == []  # it previews at the gate, not at save

    transport.outputs["publish"] = [{"url": "https://hooks.example/m/1", "id": "m1"}]
    sent = {"channel": "#news", "text": "Hi"}
    params = {**config["params"], "input_path": "/runs/r/post.md"}
    call = connectors.run_stage(
        W, "run_1", "stage_1", {**config, "params": params, "confirmed": sent}
    )
    assert transport.calls[-1][1:3] == (
        "publish",
        {"fields": sent, "idempotency_key": "stage_1", "input_path": "/runs/r/post.md"},
    )
    assert connectors.publish_result(call) == {
        "status": "published",
        "links": {"webhook": "https://hooks.example/m/1"},
        "id": "m1",
        "failed": {},
    }


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        (ConnectorError("webhook: could not start"), "webhook preview failed: webhook: could"),
        ({"nothing": True}, "preview answered without fields"),
        ({"fields": [{"label": "No key"}]}, "preview answered a field without a key"),
        ({"fields": [], "problems": ["no channel"]}, "no channel"),
    ],
)
def test_a_preview_that_cannot_be_had_is_a_problem(
    connectors: Connectors, transport: FakeTransport, answer: Any, problem: str
) -> None:
    poster(connectors)
    transport.outputs["preview"] = [answer]
    shown = connectors.publish_preview(W, {"calls": "webhook", "params": {}})
    assert shown is not None and any(problem in p for p in shown["problems"])
