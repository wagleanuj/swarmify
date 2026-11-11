"""The Meta connector: its Sign-in finish (`signed_in`) and per-call account lookup against a fake
Graph API (httpx's MockTransport, no network), then the package installed through the real
install path, given to a world as a default and answering over stdio MCP in its sandbox."""

import json
import struct
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import meta
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from swarmify_connectors import oauth as core_oauth
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_workflows import ConnectorRef
from swarmify_workflows.parse import parse_workflow
from swarmify_workflows.validate import validate

HERE = Path(__file__).parent


def page(n: int, instagram: bool = True) -> dict[str, Any]:
    linked = {"instagram_business_account": {"id": f"ig{n}"}} if instagram else {}
    return {"id": f"p{n}", "name": f"Page {n}", "access_token": f"page-tok-{n}", **linked}


class FakeMeta:
    """The Graph API: the long-lived exchange, `me/accounts` answering `pages`, `me` as the
    Page a Page token is for, an Instagram Reels container and the Page's Reels upload.
    `refused` answers every call with Meta's invalid-token error; `down` fails the Page's."""

    def __init__(
        self,
        pages: list[dict[str, Any]],
        refused: bool = False,
        statuses: list[dict[str, str]] | None = None,
        down: bool = False,
    ) -> None:
        self.pages = pages
        self.down = down
        self.broken: set[str] = set()  # paths that answer a server error
        self.video_status: dict[str, Any] = {"uploading_phase": {"status": "complete"}}
        self.refused = refused
        self.statuses = statuses or [{"status_code": "FINISHED"}]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.refused:
            error = {"message": "Error validating access token", "type": "OAuthException"}
            return httpx.Response(400, json={"error": {**error, "code": 190}})
        path = request.url.path
        if path in self.broken:
            return httpx.Response(500, json={"error": {"message": "try later", "code": 2}})
        if path == "/v26.0/oauth/access_token":
            return httpx.Response(200, json={"access_token": "long-tok", "expires_in": 5183944})
        if path == "/v26.0/me/accounts":
            return httpx.Response(200, json={"data": self.pages})
        if path == "/v26.0/me":
            return httpx.Response(
                200, json={"id": "p1", "instagram_business_account": {"id": "ig1"}}
            )
        if path == "/v26.0/ig1/media" and request.method == "GET":  # the newest posts
            caption = "Owls\n\n#owls"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "m0", "caption": "older"},
                        {"id": "m1", "caption": caption},
                    ]
                },
            )
        if path == "/v26.0/ig1/media":  # an Instagram Reels container, uploaded resumably
            uri = "https://rupload.facebook.com/ig-api-upload/v26.0/c1"
            return httpx.Response(200, json={"id": "c1", "uri": uri})
        if request.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if path == "/v26.0/c1":  # each status read answers the next status, the last one stays
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={**status, "id": "c1"})
        if path == "/v26.0/ig1/media_publish":
            return httpx.Response(200, json={"id": "m1"})
        if path == "/v26.0/m1":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/AbC/"})
        if path == "/v26.0/v1":
            return httpx.Response(200, json={"status": self.video_status, "id": "v1"})
        if path == "/v26.0/p1/video_reels":
            if self.down:
                return httpx.Response(500, json={"error": {"message": "try later", "code": 2}})
            if dict(parse_qsl(request.content.decode()))["upload_phase"] == "start":
                url = "https://rupload.facebook.com/video-upload/v26.0/v1"
                return httpx.Response(200, json={"video_id": "v1", "upload_url": url})
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": "unknown path", "code": 803}})

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def sign_in(fake: FakeMeta, env: dict[str, str] | None = None) -> str:
    with fake.http() as http:
        return meta.page_token(http, env or {}, "short-tok", "app-id", "app-secret")


# signed_in: the long-lived exchange, then the Page to keep


def test_the_one_page_with_instagram_is_kept_after_the_long_lived_exchange() -> None:
    fake = FakeMeta([page(1, instagram=False), page(2)])
    assert sign_in(fake) == "page-tok-2"
    exchange, accounts = fake.requests
    assert exchange.method == "GET" and exchange.url.path == "/v26.0/oauth/access_token"
    assert dict(exchange.url.params) == {
        "grant_type": "fb_exchange_token",
        "client_id": "app-id",
        "client_secret": "app-secret",
        "fb_exchange_token": "short-tok",
    }
    assert accounts.url.params["fields"] == "id,name,access_token,instagram_business_account"
    assert accounts.headers["authorization"] == "OAuth long-tok"  # the long-lived token


@pytest.mark.parametrize("named", ["Page 3", "p3"])
def test_with_several_pages_the_page_setting_picks_one_by_name_or_id(named: str) -> None:
    assert sign_in(FakeMeta([page(1), page(2), page(3)]), {"META_PAGE": named}) == "page-tok-3"


def test_with_several_pages_and_no_setting_it_fails_naming_them() -> None:
    with pytest.raises(ToolError) as several:
        sign_in(FakeMeta([page(1), page(2), page(3, instagram=False)]))
    message = str(several.value)
    assert "2 Pages have a linked Instagram account: Page 1 (p1), Page 2 (p2)" in message
    assert "set the Facebook Page Setting" in message and "Page 3" not in message
    with pytest.raises(ToolError, match=r"no Page 'Page 9' among .*Page 1 \(p1\), Page 2 \(p2\)"):
        sign_in(FakeMeta([page(1), page(2)]), {"META_PAGE": "Page 9"})


def test_a_page_without_a_linked_instagram_account_is_refused() -> None:
    lonely = FakeMeta([page(1), page(2, instagram=False)])
    with pytest.raises(ToolError, match=r"the Page Page 2 \(p2\) has no linked Instagram account"):
        sign_in(lonely, {"META_PAGE": "p2"})
    with pytest.raises(ToolError, match=r"none of this account's Pages .*: Page 1 \(p1\)"):
        sign_in(FakeMeta([page(1, instagram=False)]))
    with pytest.raises(ToolError, match="manages no Page"):
        sign_in(FakeMeta([]))


def test_meta_refusing_the_token_is_an_auth_failure_that_never_shows_a_token() -> None:
    """The word the core reads to mark the account for sign-in again (oauth.is_auth_failure)."""
    with pytest.raises(ToolError) as refused:
        sign_in(FakeMeta([page(1)], refused=True))
    assert core_oauth.is_auth_failure(str(refused.value))
    assert "Error validating access token" in str(refused.value)
    assert "tok" not in str(refused.value).replace("token", "")
    with FakeMeta([page(1)], refused=True).http() as http:
        with pytest.raises(ToolError) as later:
            meta.account_of(http, {"META_ACCESS_TOKEN": "page-tok-1"})
    assert core_oauth.is_auth_failure(str(later.value))
    with FakeMeta([]).http() as http:
        with pytest.raises(ToolError) as unsigned:
            meta.account_of(http, {})
    assert core_oauth.is_auth_failure(str(unsigned.value))


def test_other_meta_errors_are_plain_tool_errors() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "try later", "code": 2}})

    with httpx.Client(transport=httpx.MockTransport(down)) as http:
        with pytest.raises(ToolError, match=r"long-lived token failed \(500\): try later") as e:
            meta.page_token(http, {}, "short-tok", "app-id", "app-secret")
    assert not core_oauth.is_auth_failure(str(e.value))


# At each call: the Page and its Instagram account, read with the Page token


def test_each_call_reads_the_page_and_its_instagram_account_with_the_page_token() -> None:
    fake = FakeMeta([])
    with fake.http() as http:
        assert meta.account_of(http, {"META_ACCESS_TOKEN": "page-tok-1"}) == {
            "page": "p1",
            "instagram": "ig1",
        }
    (me,) = fake.requests
    assert me.url.path == "/v26.0/me" and me.url.params["fields"] == "id,instagram_business_account"
    assert me.headers["authorization"] == "OAuth page-tok-1"


# What goes out: the preview a gate shows

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def stage(**params: Any) -> dict[str, Any]:
    return {"calls": "meta", "input": "short.mp4", "params": {"platforms": ["instagram"], **params}}


def preview(
    config: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The preview of the stage, as the core asks for it."""
    inputs = [config["input"]] if config.get("input") else []
    args = {"inputs": inputs, "params": config["params"], "metadata": metadata, "edits": edits}
    return meta.preview_of(args, NOW)


def values(shown: dict[str, Any]) -> dict[str, Any]:
    return {f["key"]: f["value"] for f in shown["fields"]}


def test_the_preview_is_what_will_go_out() -> None:
    shown = preview(stage(title="Why owls turn", hashtags="#owls birds", cover_at=1.5))
    assert shown["problems"] == []
    assert [(f["key"], f["editable"]) for f in shown["fields"]] == [
        ("platforms", False),
        ("caption", True),
        ("hashtags", True),
        ("visibility", False),
        ("schedule", False),
        ("trial", False),
        ("share_to_feed", False),
        ("cover_at", False),
        ("video", False),
    ]
    assert values(shown) == {
        "platforms": ["instagram"],
        "caption": "Why owls turn",  # the title, as there is no description
        "hashtags": "#owls #birds",
        "visibility": "public",
        "schedule": None,
        "trial": False,
        "share_to_feed": True,
        "cover_at": 1.5,
        "video": "short.mp4",
    }


def test_the_metadata_wins_over_params_and_the_gate_edits_the_caption_and_hashtags() -> None:
    metadata = {"title": "T", "description": "Its words", "hashtags": ["#a"], "tags": "x, y"}
    shown = values(preview(stage(description="d", hashtags="#b"), metadata=metadata))
    assert (shown["caption"], shown["hashtags"]) == ("Its words", "#a")
    assert "tags" not in shown  # Instagram has none
    edits = {"caption": "Edited", "hashtags": "#c #d", "visibility": "draft"}
    shown = values(preview(stage(), metadata=metadata, edits=edits))
    assert (shown["caption"], shown["hashtags"], shown["visibility"]) == (
        "Edited",
        "#c #d",
        "public",  # only editable fields take an edit
    )


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (stage(description="x" * 2_196, hashtags="#ab"), "the caption is 2201 characters"),
        (stage(hashtags=[f"t{n}" for n in range(31)]), "31 hashtags, at most 30"),
        (stage(description="#a " * 20, hashtags=[f"t{n}" for n in range(11)]), "31 hashtags"),
        (stage(hashtags=["two words"]), "not #two words"),
        (stage(hashtags="#owls! #fine"), "not #owls!"),
        (stage(description=" ".join(f"@u{n}" for n in range(21))), "21 @mentions, at most 20"),
        (stage(schedule="2026-11-04T09:00:00Z"), "Instagram cannot schedule a post"),
        (stage(visibility="draft"), "Instagram has no drafts"),
    ],
)
def test_the_preview_names_every_instagram_problem(config: dict[str, Any], problem: str) -> None:
    problems = preview(config)["problems"]
    assert any(p.startswith("Instagram: ") and problem in p for p in problems), problems


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ({**stage(), "input": None}, "needs an input"),
        (stage(platforms=["tiktok"]), "platforms are instagram and facebook, got tiktok"),
        (stage(visibility="unlisted"), "visibility is one of public, draft"),
        (stage(trial="yes"), "trial is true or false"),
        (stage(cover_at=-1), "cover_at is a number of seconds"),
        (stage(schedule="next tuesday"), "not an ISO 8601 time"),
    ],
)
def test_the_preview_names_every_other_problem(config: dict[str, Any], problem: str) -> None:
    assert any(problem in p for p in preview(config)["problems"])


def test_twenty_mentions_and_thirty_hashtags_pass() -> None:
    mentions = " ".join(f"@u{n}" for n in range(20)) + " mail@example.com"
    config = stage(description=mentions, hashtags=[f"t{n}" for n in range(30)])
    assert preview(config)["problems"] == []


# Instagram publish: container, upload, wait, publish


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00video")
    return path


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "POLL_SECONDS", 0)


def post(fake: FakeMeta, video: Path, **params: Any) -> dict[str, Any]:
    """Publish the values a gate confirmed for `stage(**params)`."""
    fields = values(preview(stage(**params)))
    with fake.http() as http:
        return meta.publish_fields(http, {"META_ACCESS_TOKEN": "page-tok-1"}, fields, str(video))


def test_a_reel_is_created_uploaded_waited_for_and_published_in_order(video: Path) -> None:
    fake = FakeMeta([], statuses=[{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}])
    out = post(fake, video, description="Why owls turn.", hashtags="#owls")
    assert out == {
        "status": "published",
        "links": {"instagram": "https://www.instagram.com/reel/AbC/"},
        "id": "m1",
        "failed": {},
        "public": True,
    }
    assert [(r.method, r.url.host, r.url.path) for r in fake.requests] == [
        ("GET", "graph.facebook.com", "/v26.0/me"),
        ("POST", "graph.facebook.com", "/v26.0/ig1/media"),
        ("POST", "rupload.facebook.com", "/ig-api-upload/v26.0/c1"),
        ("GET", "graph.facebook.com", "/v26.0/c1"),
        ("GET", "graph.facebook.com", "/v26.0/c1"),
        ("POST", "graph.facebook.com", "/v26.0/ig1/media_publish"),
        ("GET", "graph.facebook.com", "/v26.0/m1"),
    ]
    assert all(r.headers["authorization"] == "OAuth page-tok-1" for r in fake.requests)
    _, container, upload, status, _, published, link = fake.requests
    assert dict(parse_qsl(container.content.decode())) == {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "Why owls turn.\n\n#owls",
        "share_to_feed": "true",
    }
    assert (upload.headers["offset"], upload.headers["file_size"]) == ("0", "6")
    assert upload.content == b"\x00video"  # the file itself, no public URL
    assert status.url.params["fields"] == "status_code"
    assert dict(parse_qsl(published.content.decode())) == {"creation_id": "c1"}
    assert link.url.params["fields"] == "permalink"


def test_trial_share_to_feed_and_the_cover_go_to_the_container_as_given(video: Path) -> None:
    fake = FakeMeta([])
    post(fake, video, title="T", trial=True, share_to_feed=False, cover_at=2.5)
    form = dict(parse_qsl(fake.requests[1].content.decode()))
    assert (form["caption"], form["share_to_feed"], form["thumb_offset"]) == ("T", "false", "2500")
    assert json.loads(form["trial_params"]) == {"graduation_strategy": "MANUAL"}


@pytest.mark.parametrize("state", ["ERROR", "EXPIRED"])
def test_a_container_that_fails_processing_reports_a_clear_error(video: Path, state: str) -> None:
    fake = FakeMeta([], statuses=[{"status_code": state, "status": "Error: unsupported codec"}])
    with pytest.raises(ToolError) as failed:
        post(fake, video, title="T")
    assert str(failed.value) == (
        f"instagram: Instagram could not process the video ({state}): Error: unsupported codec"
    )
    assert not any(r.url.path.endswith("/media_publish") for r in fake.requests)


def test_a_container_still_processing_gives_up_after_its_tries(
    video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(meta, "POLL_TRIES", 3)
    fake = FakeMeta([], statuses=[{"status_code": "IN_PROGRESS"}])
    with pytest.raises(ToolError, match="still processing the video"):
        post(fake, video, title="T")
    assert sum(r.url.path == "/v26.0/c1" for r in fake.requests) == 3


def test_publish_errors_are_tool_errors_and_a_refused_token_an_auth_failure(
    video: Path, tmp_path: Path
) -> None:
    with pytest.raises(ToolError, match="gone.mp4 does not exist"):
        post(FakeMeta([]), tmp_path / "gone.mp4", title="T")
    with pytest.raises(ToolError) as refused:
        post(FakeMeta([], refused=True), video, title="T")
    assert core_oauth.is_auth_failure(str(refused.value))


# The Facebook Page: preview problems, then start, upload, finish


def mp4(path: Path, seconds: float, version: int = 0) -> Path:
    """A file with just the boxes that hold an MP4's length: ftyp, then moov/mvhd."""
    if version == 1:
        mvhd = struct.pack(">B3xQQIQ", 1, 0, 0, 1000, round(seconds * 1000))
    else:
        mvhd = struct.pack(">B3xIIII", 0, 0, 0, 1000, round(seconds * 1000))
    mvhd = struct.pack(">I4s", 8 + len(mvhd) + 80, b"mvhd") + mvhd + bytes(80)
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    path.write_bytes(struct.pack(">I4s8x", 16, b"ftyp") + moov)
    return path


@pytest.mark.parametrize("version", [0, 1])
def test_the_length_of_an_mp4_is_read_from_its_header(tmp_path: Path, version: int) -> None:
    assert meta.seconds_of(mp4(tmp_path / "a.mp4", 91.5, version)) == 91.5
    (tmp_path / "raw.bin").write_bytes(b"\x00video")
    assert meta.seconds_of(tmp_path / "raw.bin") is None


def test_platforms_default_to_both() -> None:
    config = {"calls": "meta", "input": "short.mp4", "params": {"title": "T"}}
    shown = preview(config)
    assert (shown["problems"], values(shown)["platforms"]) == ([], ["instagram", "facebook"])


SOON = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
LATER = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"schedule": "2026-11-02T09:00:00Z"}, "the schedule 2026-11-02T09:00:00Z has already"),
        ({"schedule": SOON}, f"the schedule {SOON} is not 10 minutes to 29 days ahead"),
        ({"schedule": LATER}, "is not 10 minutes to 29 days ahead"),
        ({"schedule": "+1d 09:00", "visibility": "draft"}, "a draft cannot be scheduled"),
    ],
)
def test_the_preview_names_every_facebook_problem(params: dict[str, Any], problem: str) -> None:
    problems = preview(stage(platforms=["facebook"], **params))["problems"]
    assert any(p.startswith("Facebook: ") and problem in p for p in problems), problems


def test_a_facebook_draft_or_schedule_in_its_window_is_no_problem() -> None:
    for params in ({"visibility": "draft"}, {"schedule": "+1d 09:00"}, {"schedule": "13:00"}):
        assert preview(stage(platforms=["facebook"], **params))["problems"] == []
    both = preview(stage(platforms=["instagram", "facebook"], schedule="+1d 09:00"))["problems"]
    assert both == ["Instagram: Instagram cannot schedule a post; remove the schedule"]


def test_a_video_over_90_seconds_is_a_problem_for_facebook_only(tmp_path: Path) -> None:
    long = str(mp4(tmp_path / "long.mp4", 91))
    both = {**stage(platforms=["instagram", "facebook"]), "input": long}
    assert preview(both)["problems"] == [
        "Facebook: the video is 91 seconds, a Facebook Reel is at most 90"
    ]
    assert preview({**stage(), "input": long})["problems"] == []
    short = str(mp4(tmp_path / "short.mp4", 90))
    assert preview({**both, "input": short})["problems"] == []


def test_publish_to_both_posts_on_each_platform_and_answers_both_links(video: Path) -> None:
    fake = FakeMeta([])
    out = post(fake, video, platforms=["instagram", "facebook"], title="Owls", hashtags="#owls")
    assert out == {
        "status": "published",
        "links": {
            "instagram": "https://www.instagram.com/reel/AbC/",
            "facebook": "https://www.facebook.com/reel/v1",
        },
        "id": "m1",
        "failed": {},
        "public": True,
    }
    facebook = [r for r in fake.requests if "video" in r.url.path]
    assert [(r.method, r.url.host, r.url.path) for r in facebook] == [
        ("POST", "graph.facebook.com", "/v26.0/p1/video_reels"),
        ("POST", "rupload.facebook.com", "/video-upload/v26.0/v1"),
        ("POST", "graph.facebook.com", "/v26.0/p1/video_reels"),
    ]
    start, upload, finish = facebook
    assert all(r.headers["authorization"] == "OAuth page-tok-1" for r in facebook)
    assert dict(parse_qsl(start.content.decode())) == {"upload_phase": "start"}
    assert (upload.headers["offset"], upload.headers["file_size"]) == ("0", "6")
    assert upload.content == b"\x00video"
    assert dict(parse_qsl(finish.content.decode())) == {
        "upload_phase": "finish",
        "video_id": "v1",
        "video_state": "PUBLISHED",
        "description": "Owls\n\n#owls",
    }


def test_draft_and_scheduled_states_reach_facebook_as_asked(video: Path) -> None:
    fake = FakeMeta([])
    out = post(fake, video, platforms=["facebook"], title="T", visibility="draft")
    assert (out["status"], out["links"], out["id"]) == (
        "published",
        {"facebook": "https://www.facebook.com/reel/v1"},
        "v1",
    )
    assert out["public"] is False and "scheduled_for" not in out  # a draft only admins see
    assert dict(parse_qsl(fake.requests[-1].content.decode()))["video_state"] == "DRAFT"
    fake = FakeMeta([])
    out = post(fake, video, platforms=["facebook"], title="T", schedule="+1d 09:00")
    finish = dict(parse_qsl(fake.requests[-1].content.decode()))
    assert (out["status"], out["public"]) == ("scheduled", True)
    assert out["scheduled_for"] == "2026-11-04T09:00:00Z"
    assert finish["video_state"] == "SCHEDULED"
    assert finish["scheduled_publish_time"] == str(
        int(datetime(2026, 11, 4, 9, tzinfo=UTC).timestamp())
    )


def test_a_long_video_still_posts_to_instagram_and_fails_on_facebook(tmp_path: Path) -> None:
    long = mp4(tmp_path / "long.mp4", 91)
    out = post(FakeMeta([]), long, platforms=["instagram", "facebook"], title="T")
    assert out["links"] == {"instagram": "https://www.instagram.com/reel/AbC/"}
    assert out["failed"] == {"facebook": "the video is 91 seconds, a Facebook Reel is at most 90"}
    fake = FakeMeta([])
    assert post(fake, long, title="T")["links"] == {
        "instagram": "https://www.instagram.com/reel/AbC/"
    }


def test_one_platform_failing_still_answers_the_others_link(video: Path) -> None:
    out = post(FakeMeta([], down=True), video, platforms=["instagram", "facebook"], title="T")
    assert out["links"] == {"instagram": "https://www.instagram.com/reel/AbC/"}
    assert out["failed"] == {"facebook": "posting the Reel to the Page failed (500): try later"}
    broken = FakeMeta([], statuses=[{"status_code": "ERROR"}])
    out = post(broken, video, platforms=["instagram", "facebook"], title="T")
    assert out["links"] == {"facebook": "https://www.facebook.com/reel/v1"}
    assert out["failed"] == {"instagram": "Instagram could not process the video (ERROR)"}
    assert out["id"] == "v1"
    with pytest.raises(ToolError, match="^facebook: posting the Reel"):
        post(FakeMeta([], down=True), video, platforms=["facebook"], title="T")


# Retries: the record in the stage's folder, named after the Idempotency Key


def retried(fake: FakeMeta, video: Path, key: str, **params: Any) -> dict[str, Any]:
    """Publish as the stage `post` of run `r` does, its input another stage's file."""
    fields = values(preview(stage(title="Owls", hashtags="#owls", **params)))
    with fake.http() as http:
        env = {"META_ACCESS_TOKEN": "page-tok-1"}
        return meta.publish_fields(http, env, fields, str(video), key)


@pytest.fixture
def run_video(tmp_path: Path) -> Path:
    (tmp_path / "runs" / "r" / "post").mkdir(parents=True)  # the core makes the stage's folder
    path = tmp_path / "runs" / "r" / "render" / "artifacts" / "short.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00video")
    return path


def calls(fake: FakeMeta) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in fake.requests]


@pytest.mark.parametrize("state", ["FINISHED", "PUBLISHED"])
def test_a_retry_after_instagrams_upload_publishes_the_same_container(
    run_video: Path, state: str
) -> None:
    fake = FakeMeta([])
    fake.broken = {"/v26.0/ig1/media_publish"}
    with pytest.raises(ToolError, match="publishing the Reel failed"):
        retried(fake, run_video, "post")
    record = run_video.parents[2] / "post" / "meta-post.json"
    assert json.loads(record.read_text()) == {"instagram": {"container": "c1"}}
    fake.broken, fake.requests, fake.statuses = set(), [], [{"status_code": state}]
    out = retried(fake, run_video, "post")
    assert out["links"] == {"instagram": "https://www.instagram.com/reel/AbC/"}
    assert not any(r.url.host == "rupload.facebook.com" for r in fake.requests)  # nothing uploaded
    assert ("POST", "/v26.0/ig1/media") not in calls(fake)
    if state == "FINISHED":  # the same container, published now
        (published,) = [r for r in fake.requests if r.url.path.endswith("/media_publish")]
        assert dict(parse_qsl(published.content.decode())) == {"creation_id": "c1"}
    else:  # Meta published it before failing: found by its caption, not published again
        assert ("POST", "/v26.0/ig1/media_publish") not in calls(fake)
    fake.requests = []
    assert retried(fake, run_video, "post")["links"] == out["links"]
    assert calls(fake) == [("GET", "/v26.0/me")]  # a finished post is answered as recorded


def test_a_retry_after_facebooks_start_finishes_or_returns_the_same_video(
    run_video: Path,
) -> None:
    fake = FakeMeta([])
    fake.broken = {"/video-upload/v26.0/v1"}
    with pytest.raises(ToolError, match="uploading the video failed"):
        retried(fake, run_video, "post", platforms=["facebook"])
    fake.broken, fake.requests = set(), []
    fake.video_status = {"uploading_phase": {"status": "not_started"}}
    out = retried(fake, run_video, "post", platforms=["facebook"])
    assert out["links"] == {"facebook": "https://www.facebook.com/reel/v1"}
    assert calls(fake) == [
        ("GET", "/v26.0/me"),
        ("GET", "/v26.0/v1"),  # its status: the upload never completed
        ("POST", "/video-upload/v26.0/v1"),
        ("POST", "/v26.0/p1/video_reels"),
    ]
    assert dict(parse_qsl(fake.requests[-1].content.decode()))["upload_phase"] == "finish"
    # Meta finished it but the answer was lost: the same video is answered, nothing posted
    record = run_video.parents[2] / "post" / "meta-post.json"
    record.write_text(json.dumps({"facebook": {"video": "v1"}}))
    fake.requests = []
    fake.video_status = {"publishing_phase": {"status": "complete"}}
    assert retried(fake, run_video, "post", platforms=["facebook"])["links"] == out["links"]
    assert calls(fake) == [("GET", "/v26.0/me"), ("GET", "/v26.0/v1")]


def test_a_retry_after_instagram_posted_and_facebook_failed_redoes_only_facebook(
    run_video: Path,
) -> None:
    fake = FakeMeta([], down=True)
    both = ["instagram", "facebook"]
    out = retried(fake, run_video, "post", platforms=both)
    assert list(out["failed"]) == ["facebook"]
    fake.down, fake.requests = False, []
    out = retried(fake, run_video, "post", platforms=both)
    assert (out["links"], out["failed"], out["id"]) == (
        {
            "instagram": "https://www.instagram.com/reel/AbC/",
            "facebook": "https://www.facebook.com/reel/v1",
        },
        {},
        "m1",
    )
    assert not any("ig1" in r.url.path or "c1" in r.url.path for r in fake.requests)
    assert calls(fake).count(("POST", "/v26.0/p1/video_reels")) == 2  # start and finish


def test_a_fresh_stage_never_reads_another_stages_record(run_video: Path) -> None:
    retried(FakeMeta([]), run_video, "post")
    fake = FakeMeta([])
    retried(fake, run_video, "post.1")  # the stage visited again: a new key
    assert ("POST", "/v26.0/ig1/media") in calls(fake)
    folder = run_video.parents[2] / "post"
    assert sorted(p.name for p in folder.iterdir()) == ["meta-post.1.json", "meta-post.json"]


def test_without_a_run_folder_nothing_is_recorded(video: Path, tmp_path: Path) -> None:
    retried(FakeMeta([]), video, "post")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["short.mp4"]


# The Manifest, and the package installed as Swarmify does at startup


# outcomes: views per Platform, for every post with an Instagram or Facebook link


IG_METRICS = "views,reach,ig_reels_avg_watch_time"
REEL = {"instagram_reach": 6, "instagram_avg_watch_seconds": 8.25}


def insights(gone: tuple[str, ...] = ()) -> Any:
    """The Graph API's reads for Outcomes: `me`, the Instagram account's media in pages of two,
    and views of each media and Reel; `gone` answers Meta's error for that media or Reel."""
    media = [
        {"id": "m1", "permalink": "https://www.instagram.com/reel/AbC/"},
        {"id": "m2", "permalink": "https://www.instagram.com/p/Xy_z/"},
        {"id": "m3", "permalink": "https://www.instagram.com/reel/Old/"},
    ]
    seen: list[httpx.Request] = []

    def graph(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path, q = request.url.path.removeprefix("/v26.0/"), request.url.params
        if path == "me":
            return httpx.Response(
                200, json={"id": "p1", "instagram_business_account": {"id": "ig1"}}
            )
        if path == "ig1/media":
            start = int(q.get("after") or 0)
            more = {"cursors": {"after": str(start + 2)}, "next": "x"} if start + 2 < 3 else {}
            return httpx.Response(200, json={"data": media[start : start + 2], "paging": more})
        thing = path.split("/")[0]
        if thing in gone:
            return httpx.Response(400, json={"error": {"message": "no such object", "code": 100}})
        if path.endswith("/insights") and q["metric"] == IG_METRICS:
            if thing == "m2":  # a photo post: Meta refuses the Reels-only metrics
                return httpx.Response(400, json={"error": {"message": "bad metric", "code": 100}})
            views = {"name": "views", "values": [{"value": {"m1": 7, "m3": 1}[thing]}]}
            reach = {"name": "reach", "values": [{"value": 6}]}
            watch = {"name": "ig_reels_avg_watch_time", "values": [{"value": 8250}]}
            return httpx.Response(200, json={"data": [views, reach, watch]})
        if path.endswith("/insights") and q["metric"] == "views":
            views = {"name": "views", "values": [{"value": {"m1": 7, "m2": 5, "m3": 1}[thing]}]}
            return httpx.Response(200, json={"data": [views]})
        if path.endswith("/video_insights") and q["metric"] == "fb_reels_total_plays":
            plays = {"name": "fb_reels_total_plays", "values": [{"value": 4}]}
            return httpx.Response(200, json={"data": [plays]})
        return httpx.Response(404, json={"error": {"message": "unknown path", "code": 803}})

    graph.seen = seen  # type: ignore[attr-defined]
    return graph


BOTH = {
    "connector": "meta",
    "id": "m1",
    "links": {
        "instagram": "https://www.instagram.com/reel/AbC/",
        "facebook": "https://www.facebook.com/reel/v1",
    },
}


def outcomes(posts: list[dict[str, Any]], gone: tuple[str, ...] = ()) -> dict[str, Any]:
    with httpx.Client(transport=httpx.MockTransport(insights(gone))) as http:
        return meta.outcomes_of(http, {"META_ACCESS_TOKEN": "page-tok"}, posts)


def test_a_post_without_reels_retention_still_answers_its_views() -> None:
    photo = {**BOTH, "id": "m2", "links": {"instagram": "https://www.instagram.com/p/Xy_z/"}}
    assert outcomes([photo]) == {"m2": {"views": 5, "instagram_views": 5}}


def test_outcomes_answer_views_per_platform_and_their_sum() -> None:
    facebook_only = {
        "connector": "meta",
        "id": "v2",
        "links": {"facebook": "https://www.facebook.com/reel/v2"},
    }
    instagram_only = {**BOTH, "id": "m3", "links": {"instagram": "https://instagram.com/reel/Old"}}
    assert outcomes([BOTH, facebook_only, instagram_only]) == {
        "m1": {"views": 11, "instagram_views": 7, "facebook_views": 4, **REEL},
        "v2": {"views": 4, "facebook_views": 4},
        # found on the media list's second page
        "m3": {"views": 1, "instagram_views": 1, **REEL},
    }


def test_outcomes_claim_other_connectors_posts_with_instagram_or_facebook_links() -> None:
    posts = [
        {
            "connector": "upload_post",
            "id": "job_1",
            "links": {
                "instagram": "https://www.instagram.com/p/Xy_z/",
                "tiktok": "https://www.tiktok.com/@me/video/1",
            },
        },
        {"connector": "youtube", "id": "yt1", "links": {"youtube": "https://youtu.be/abc123"}},
        {"connector": "upload_post", "id": "job_2", "links": {}},
        {"connector": "clicks", "id": "p1", "links": "not a map"},
        {  # not the world's Instagram account: its media list has no such post
            "connector": "upload_post",
            "id": "job_3",
            "links": {"instagram": "https://www.instagram.com/reel/Unknown/"},
        },
    ]
    assert outcomes(posts) == {"job_1": {"views": 5, "instagram_views": 5}}
    graph = insights()
    with httpx.Client(transport=httpx.MockTransport(graph)) as http:
        assert meta.outcomes_of(http, {}, posts[1:4]) == {}  # nothing to ask Meta
    assert graph.seen == []


def test_meta_failing_for_one_post_leaves_the_others() -> None:
    other = {**BOTH, "id": "m2", "links": {"instagram": "https://www.instagram.com/p/Xy_z/"}}
    assert outcomes([BOTH, other], gone=("m1",)) == {
        "m1": {"views": 4, "facebook_views": 4},
        "m2": {"views": 5, "instagram_views": 5},
    }
    assert outcomes([BOTH, other], gone=("m1", "v1")) == {"m2": {"views": 5, "instagram_views": 5}}


def test_outcomes_with_a_refused_token_are_an_auth_failure() -> None:
    with FakeMeta([page(1)], refused=True).http() as http, pytest.raises(ToolError) as refused:
        meta.outcomes_of(http, {"META_ACCESS_TOKEN": "page-tok"}, [BOTH])
    assert str(refused.value).startswith("auth_failed")


def test_the_manifest_declares_a_world_sign_in_the_package_finishes() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["stage_types"]) == ("meta", "publish", ["connector"])
    assert (m["capabilities"], m["primary_metric"]) == (["publish", "outcomes"], "views")
    login, account, chosen = m["config"]
    assert (login["key"], login["label"], login["type"]) == (
        "meta_config_id",
        "Login configuration ID",
        "string",
    )
    assert (login["scope"], login.get("secret"), bool(login["help"])) == ("shared", None, True)
    assert (account["key"], account["type"], account["env"]) == ("meta", "oauth", meta.TOKEN)
    assert (account["scope"], chosen["scope"]) == ("world", "world")
    assert (chosen["key"], chosen["type"], chosen["env"]) == ("page", "string", meta.PAGE)
    assert not chosen.get("required")
    o = account["oauth"]
    assert (o["authorize_url"], o["token_url"]) == (
        "https://www.facebook.com/v26.0/dialog/oauth",
        "https://graph.facebook.com/v26.0/oauth/access_token",
    )
    assert (o["pkce"], o["finish"], o.get("scopes")) == (False, True, None)
    assert (o["authorize_settings"], o.get("authorize_params")) == (
        {"config_id": "meta_config_id"},
        None,
    )
    url = core_oauth.authorize_url(
        account, "app-id", "https://box.ts.net/cb", "st", "v" * 43, {"config_id": "123"}
    )
    assert "config_id=123" in url and "code_challenge" not in url and "scope=" not in url
    assert m["permissions"] == {"network": ["graph.facebook.com", "rupload.facebook.com"]}


def registry(data: Path) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), McpTransport(), Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


def test_the_installed_package_answers_over_stdio_in_its_sandbox(tmp_path: Path) -> None:
    connectors = registry(tmp_path)
    assert connectors.install_defaults()["meta"] == "1.0.2"
    connectors.offer_defaults("w")
    found = connectors.find("w", "meta")
    assert found is not None and found.version == "1.0.2"
    assert sorted(t.name for t in connectors.transport.list_tools(found, {})) == [
        "account",
        "outcomes",
        "preview",
        "publish",
        "signed_in",
    ]
    env = connectors._env(found, complete=False)
    # no token from Meta: refused before reaching it, as the core's Sign-in error
    with pytest.raises(ConnectorError) as refused:
        arguments = {"access_token": "", "client_id": "app-id", "client_secret": "s"}
        connectors._tool(found, "signed_in", arguments, env)
    assert core_oauth.is_auth_failure(str(refused.value))

    # the preview a gate shows comes over stdio too, and a saved stage is checked through it
    shown = connectors.publish_preview("w", stage(hashtags="#owls"), {"description": "Owls."})
    assert shown is not None and (shown["connector"], shown["problems"]) == ("meta", [])
    assert (values(shown)["caption"], values(shown)["hashtags"]) == ("Owls.", "#owls")
    assert connectors.stage_problems("w", stage(visibility="draft")) == [
        "Instagram: Instagram has no drafts; a draft is Facebook's only"
    ]
    both = {"calls": "meta", "input": "short.mp4", "params": {"schedule": "+1d 09:00"}}
    assert connectors.stage_problems("w", both) == [
        "Instagram: Instagram cannot schedule a post; remove the schedule"
    ]

    # only a Facebook draft posts privately; a stage with Instagram is always gated
    assert connectors.posts_privately("w", stage(platforms=["facebook"], visibility="draft"))
    assert not connectors.posts_privately("w", {**stage(visibility="draft"), "params": {}})
    both = stage(platforms=["instagram", "facebook"], visibility="draft")
    assert not connectors.posts_privately("w", both)


def test_a_workflow_with_an_instagram_stage_and_no_gate_is_refused_when_saved() -> None:
    m = read_manifest(HERE)
    ref = ConnectorRef("meta", "publish", frozenset({"connector"}), private_when=m["private_when"])

    def refused(params: str, gated: bool = False) -> bool:
        gate = "  - gate: review\n    ask: Watch it.\n" if gated else ""
        post = f"  - connector: post\n    calls: meta\n    input: short.mp4\n    params: {params}\n"
        model, problems = parse_workflow(f"name: w\nstages:\n{gate}{post}")
        assert model is not None and problems == []
        found = validate(model, workflow_id=None, connectors={"meta": ref}, references={})
        return "publish_without_gate" in [v.rule for v in found]

    assert refused("{platforms: [instagram], visibility: draft}")
    assert refused("{platforms: [instagram, facebook]}")
    assert not refused("{platforms: [instagram]}", gated=True)
    assert not refused("{platforms: [facebook], visibility: draft}")
