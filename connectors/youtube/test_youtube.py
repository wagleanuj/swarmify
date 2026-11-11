"""The YouTube connector: its preview of what goes out (fields, problems, hashtags, tags and the
schedule rules), the Data API calls against a fake Google (httpx's MockTransport, no network),
including the retry tag, and the package installed through the real install path, given to a
world as a default, signed in from a V1 refresh token and answering `preview` and `publish` over
stdio MCP."""

import json
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import youtube as yt
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from swarmify_connectors import oauth as core_oauth
from swarmify_connectors.fake_oauth import FakeProvider
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
ENV = {"YOUTUBE_ACCESS_TOKEN": "tok"}
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=u1"


# What goes out: the preview a gate shows


def preview(
    config: dict[str, Any],
    now: datetime = NOW,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The preview of the stage, as the core asks for it."""
    inputs = [config["input"]] if config.get("input") else []
    args = {"inputs": inputs, "params": config["params"], "metadata": metadata, "edits": edits}
    return yt.preview_of(args, now)


def values(shown: dict[str, Any]) -> dict[str, Any]:
    return {f["key"]: f["value"] for f in shown["fields"]}


def stage(**params: Any) -> dict[str, Any]:
    base = {"title": "Five hooks", "visibility": "public"}
    return {"calls": "youtube", "input": "short.mp4", "params": {**base, **params}}


def test_the_preview_is_what_will_go_out() -> None:
    shown = preview(stage(schedule="2026-11-03T10:00:00+01:00", description="d"))
    assert shown["problems"] == []
    assert [(f["key"], f["label"], f["editable"]) for f in shown["fields"]] == [
        ("title", "Title", True),
        ("description", "Description", True),
        ("hashtags", "Hashtags", True),
        ("tags", "Tags", True),
        ("visibility", "Visibility", False),
        ("made_for_kids", "Made for kids", False),
        ("schedule", "When", False),
        ("video", "Video", False),
    ]
    assert values(shown) == {
        "title": "Five hooks",
        "description": "d",
        "hashtags": "",
        "tags": "",
        "visibility": "public",
        "made_for_kids": False,
        "schedule": "2026-11-03T09:00:00Z",
        "video": "short.mp4",
    }
    assert shown["fields"][6]["hint"] == "Private until then, then public."
    assert preview(stage())["fields"][6]["hint"] == "Right after confirming."


def test_metadata_wins_over_params_and_edits_win_over_both() -> None:
    metadata = {"title": "From the script", "description": "Its words", "type": "script"}
    shown = values(preview(stage(), metadata=metadata))
    assert (shown["title"], shown["description"]) == ("From the script", "Its words")
    edits = {"title": "Edited", "visibility": "private"}  # only editable fields take an edit
    shown = values(preview(stage(), metadata=metadata, edits=edits))
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
    ],
)
def test_the_preview_names_every_problem(config: dict[str, Any], problem: str) -> None:
    assert any(problem in p for p in preview(config)["problems"])


def test_a_title_may_come_from_the_metadata_file_at_run_time() -> None:
    assert preview({"input": "short.mp4", "params": {"metadata": "script.md"}})["problems"] == []


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
    shown = preview(stage(schedule=relative))
    assert (values(shown)["schedule"], shown["problems"]) == (resolved, [])


def test_hashtags_and_tags_come_from_the_metadata_and_the_gate_may_edit_them() -> None:
    metadata = {"hashtags": ["#Owls", "nature", "#AnimalFacts"], "tags": "owls, owl facts"}
    shown = values(preview(stage(description="Why owls turn."), metadata=metadata))
    assert (shown["hashtags"], shown["tags"]) == ("#Owls #nature #AnimalFacts", "owls, owl facts")
    edits = {"hashtags": "#Owls #Birds", "tags": "owls"}
    edited = values(preview(stage(), metadata=metadata, edits=edits))
    assert (edited["hashtags"], edited["tags"]) == ("#Owls #Birds", "owls")
    assert values(preview(stage(hashtags="owls birds")))["hashtags"] == "#owls #birds"


NINE = ", ".join(["x" * 49] * 9)  # 9 tags of 49 and 8 commas: 449 characters


@pytest.mark.parametrize(
    "params",
    [
        {"hashtags": "#two words, #fine"},  # three hashtags: #two #words #fine
        {"hashtags": "#a #b #c #d #e"},
        {"tags": NINE},
        {"tags": ", ".join(["x" * 49] * 8 + ["x" * 50])},  # 450, the limit exactly
        {"description": "é" * 2_497, "hashtags": "#ok"},  # 4,994 bytes, 2 newlines, #ok: 4,999
        {"description": "é" * 2_497 + "a", "hashtags": "#ok"},  # 5,000, the limit exactly
    ],
)
def test_hashtags_and_tags_within_the_limits_pass(params: dict[str, Any]) -> None:
    assert preview(stage(**params))["problems"] == []


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"hashtags": ["two words"]}, "not #two words"),
        ({"hashtags": "#owls! #fine"}, "one word of letters, digits or _, not #owls!"),
        ({"hashtags": "#a #b #c #d #e #f"}, "6 hashtags, at most 5"),
        ({"tags": NINE + ", y"}, "the tags take 451 characters, at most 450"),
        ({"tags": ["owl facts"] * 41}, "the tags take 491 characters"),  # quoted: 11 each
        ({"description": "é" * 2_498, "hashtags": "#ok"}, "is 5001 bytes, the limit is 5000"),
    ],
)
def test_hashtags_and_tags_past_the_limits_are_problems(
    params: dict[str, Any], problem: str
) -> None:
    problems = preview(stage(**params))["problems"]
    assert any(problem in p for p in problems), problems


# The Data API, against a fake Google


class FakeGoogle:
    """`uploads` are the channel's videos, newest first, as `videos.list` part snippet answers."""

    def __init__(self, upload_status: int = 200, uploads: list[dict] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.upload_status = upload_status
        self.uploads = uploads or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
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
        return httpx.Response(200, json={"id": "vid1", "status": {"privacyStatus": "private"}})

    def starts(self) -> list[dict[str, Any]]:
        """The metadata of each upload started."""
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path.startswith("/upload")
        ]


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00video")
    return path


def post(google: FakeGoogle, video: Path, key: str = "", **confirmed: Any) -> dict[str, Any]:
    """Publish the values a gate confirmed for `stage()`, with `confirmed` over them."""
    fields = {**values(preview(stage())), **confirmed}
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        return yt.publish_fields(http, ENV, fields, str(video), key)


def test_a_scheduled_upload_is_private_with_publish_at(video: Path) -> None:
    google = FakeGoogle()
    out = post(google, video, description="d", schedule="2099-01-01T09:00:00Z")
    assert out == {"id": "vid1", "url": "https://youtu.be/vid1", "status": "scheduled"}
    start, upload = google.requests
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
    assert post(google, video, visibility="unlisted")["status"] == "published"
    status = google.starts()[0]["status"]
    assert status == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}


def test_made_for_kids_is_declared_to_youtube(video: Path) -> None:
    google = FakeGoogle()
    post(google, video, made_for_kids=True)
    assert google.starts()[0]["status"]["selfDeclaredMadeForKids"] is True


def test_hashtags_end_the_description_and_tags_go_to_youtube(video: Path) -> None:
    google = FakeGoogle()
    post(google, video, description="Why owls turn.", hashtags="#owls #birds", tags="owls, owl x")
    post(google, video, hashtags="owls")
    first, second = (s["snippet"] for s in google.starts())
    assert first == {
        "title": "Five hooks",
        "description": "Why owls turn.\n\n#owls #birds",
        "tags": ["owls", "owl x"],
    }
    assert second == {"title": "Five hooks", "description": "#owls"}


def test_a_retry_finds_the_video_already_uploaded_under_its_key(video: Path) -> None:
    earlier = {"id": "vid0", "snippet": {"tags": ["swarmify-stage_1"]}}
    other = {"id": "vidx", "snippet": {"title": "no tags"}}
    google = FakeGoogle(uploads=[other, earlier])
    out = post(google, video, "stage_1")
    assert out == {"id": "vid0", "url": "https://youtu.be/vid0", "status": "published"}
    assert [r.method for r in google.requests] == ["GET", "GET", "GET"]
    channels, items, videos = google.requests
    assert channels.url.params["mine"] == "true"
    assert items.url.params["playlistId"] == "UU1"
    assert videos.url.params["id"] == "vidx,vid0" and videos.url.params["part"] == "snippet"


def test_tags_go_beside_the_retry_tag_never_instead(video: Path) -> None:
    google = FakeGoogle(uploads=[{"id": "vidx", "snippet": {"tags": ["swarmify-stage_2"]}}])
    assert post(google, video, "stage_1", tags="owls, owl facts")["id"] == "vid1"
    post(google, video, tags=["owls"])
    assert [s["snippet"]["tags"] for s in google.starts()] == [
        ["owls", "owl facts", "swarmify-stage_1"],
        ["owls"],
    ]


def test_google_errors_and_missing_inputs_are_tool_errors(video: Path, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"uploading the video failed \(403\): quotaExceeded"):
        post(FakeGoogle(403), video)
    with pytest.raises(ToolError, match="does not exist"):
        post(FakeGoogle(), tmp_path / "nope.mp4")


def test_a_refused_or_missing_access_token_is_an_auth_failure(video: Path) -> None:
    """The word the core reads to mark the account for sign-in again (oauth.is_auth_failure)."""
    with pytest.raises(ToolError) as refused:
        post(FakeGoogle(401), video)
    assert core_oauth.is_auth_failure(str(refused.value))
    with httpx.Client(transport=httpx.MockTransport(FakeGoogle())) as http:
        with pytest.raises(ToolError) as unsigned:
            yt.channel_name(http, {})
    assert core_oauth.is_auth_failure(str(unsigned.value))


def test_view_counts_and_privacy_come_from_videos_list_in_batches_of_50() -> None:
    seen: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        ids = request.url.params["id"].split(",")
        items = [
            {
                "id": i,
                "statistics": {"viewCount": str(len(i))},
                "status": {"privacyStatus": "private" if i == "v1" else "public"},
            }
            for i in ids
            if i != "gone"
        ]
        return httpx.Response(200, json={"items": items})

    ids = [f"v{n}" for n in range(60)] + ["gone", "v1"]
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        found = yt.videos_of(http, ENV, ids)
        assert yt.videos_of(http, ENV, []) == {}
    assert found["v1"] == {"views": 2, "privacy": "private"}
    assert found["v59"] == {"views": 3, "privacy": "public"}
    assert "gone" not in found and len(found) == 60
    first, second = seen
    assert first.url.path == "/youtube/v3/videos"
    assert first.url.params["part"] == "statistics,status"
    assert len(first.url.params["id"].split(",")) == 50
    assert second.url.params["id"].split(",") == [*(f"v{n}" for n in range(50, 60)), "gone"]
    assert first.headers["authorization"] == "Bearer tok"

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "insufficientPermissions"}})

    with httpx.Client(transport=httpx.MockTransport(refused)) as http:
        with pytest.raises(ToolError, match=r"view counts failed \(403\): insufficient"):
            yt.videos_of(http, ENV, ["v1"])


def google_with_analytics(
    analytics: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """A fake Google: the Data API answers every video but gone00 at 10 views (abc123 private),
    YouTube Analytics answers as `analytics` does."""

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "youtubeanalytics.googleapis.com":
            return analytics(request)
        ids = request.url.params["id"].split(",")
        items = [
            {
                "id": i,
                "statistics": {"viewCount": "10"},
                "status": {"privacyStatus": "private" if i == "abc123" else "public"},
            }
            for i in ids
            if i != "gone00"
        ]
        return httpx.Response(200, json={"items": items})

    return google


def report(
    *metrics: str, rows: dict[str, list[float]]
) -> Callable[[httpx.Request], httpx.Response]:
    """YouTube Analytics' basic report of one video, `rows` by video id (none: no data yet)."""

    def analytics(request: httpx.Request) -> httpx.Response:
        video = request.url.params["filters"].removeprefix("video==")
        asked = request.url.params["metrics"].split(",")
        if "engagedViews" in asked and "engagedViews" not in metrics:
            return httpx.Response(400, json={"error": {"message": "Unknown identifier"}})
        headers = [{"name": m} for m in asked]
        found = rows.get(video)
        return httpx.Response(
            200, json={"columnHeaders": headers, "rows": [found[: len(asked)]] if found else []}
        )

    return analytics


POSTS = [
    {"connector": "youtube", "id": "abc123", "links": {"youtube": "https://youtu.be/abc123"}},
    {"connector": "youtube", "id": "sched1", "links": {}},  # scheduled: no link yet
    {
        "connector": "upload_post",
        "id": "job_1",
        "links": {
            "youtube": "https://www.youtube.com/shorts/short_1",
            "tiktok": "https://www.tiktok.com/@me/video/1",
            "other": "https://youtube.com/watch?v=watch-2&t=3",
        },
    },
    {"connector": "upload_post", "id": "job_9", "links": {}},
    {"connector": "clicks", "id": "p1", "links": "not a map"},
    {"connector": "upload_post", "id": "job_gone", "links": {"youtube": "https://youtu.be/gone00"}},
]


def test_outcomes_claim_every_post_that_is_a_youtube_video_whoever_posted_it() -> None:
    assert yt.claimed(POSTS) == {
        "abc123": ["abc123"],
        "sched1": ["sched1"],
        "job_1": ["short_1", "watch-2"],
        "job_gone": ["gone00"],
    }
    seen: list[httpx.Request] = []
    rows = {
        "abc123": [9, 62.5, 12, 8],
        "short_1": [10, 50.0, 10, 9],
        "watch-2": [10, 70.0, 20, 1],
    }
    analytics = report(
        "views", "averageViewPercentage", "averageViewDuration", "engagedViews", rows=rows
    )

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return analytics(request)

    with httpx.Client(transport=httpx.MockTransport(google_with_analytics(recording))) as http:
        assert yt.outcomes_of(http, ENV, POSTS, NOW) == {
            "abc123": {
                "views": 10,
                "privacy": "private",
                "youtube_avg_viewed_percent": 62.5,
                "youtube_avg_view_seconds": 12,
                "youtube_engaged_views": 8,
            },
            "sched1": {"views": 10, "privacy": "public"},  # no Analytics data yet
            "job_1": {  # its two YouTube videos: counts add up, averages are the mean
                "views": 20,
                "privacy": "public",
                "youtube_avg_viewed_percent": 60.0,
                "youtube_avg_view_seconds": 15.0,
                "youtube_engaged_views": 10,
            },
        }
        assert yt.outcomes_of(http, ENV, POSTS[3:5], NOW) == {}  # nothing to ask YouTube
    first = seen[0]
    assert first.url.path == "/v2/reports"
    assert first.url.params["ids"] == "channel==MINE"
    assert first.url.params["endDate"] == "2026-10-26"
    assert first.headers["authorization"] == "Bearer tok"


def test_a_channel_without_engaged_views_still_gets_retention() -> None:
    analytics = report(
        "views", "averageViewPercentage", "averageViewDuration", rows={"abc123": [9, 40, 6]}
    )
    with httpx.Client(transport=httpx.MockTransport(google_with_analytics(analytics))) as http:
        assert yt.outcomes_of(http, ENV, POSTS[:1], NOW) == {
            "abc123": {
                "views": 10,
                "privacy": "private",
                "youtube_avg_viewed_percent": 40,
                "youtube_avg_view_seconds": 6,
            }
        }


@pytest.mark.parametrize(
    ("reason", "notice"),
    [
        ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "Sign in to YouTube again"),
        ("insufficientPermissions", "Sign in to YouTube again"),
        ("accessNotConfigured", "enable the YouTube Analytics API"),
    ],
)
def test_retention_it_may_not_read_leaves_the_views_and_one_notice(
    reason: str, notice: str
) -> None:
    asked: list[httpx.Request] = []

    def refused(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        error = {"message": "denied", "errors": [{"reason": reason}]}
        if reason.startswith("ACCESS"):
            error = {"message": "denied", "details": [{"reason": reason}]}
        return httpx.Response(403, json={"error": error})

    with httpx.Client(transport=httpx.MockTransport(google_with_analytics(refused))) as http:
        answer = yt.outcomes_of(http, ENV, POSTS, NOW)
    assert notice in answer.pop(yt.NOTICE)
    assert answer["abc123"] == {"views": 10, "privacy": "private"}
    assert len(asked) == 1  # the first refusal stops the rest of the pass


def test_retention_of_a_video_the_channel_does_not_own_is_left_out_quietly() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "Forbidden", "errors": [{"reason": "forbidden"}]}}
        )

    with httpx.Client(transport=httpx.MockTransport(google_with_analytics(forbidden))) as http:
        assert yt.outcomes_of(http, ENV, POSTS[:1], NOW) == {
            "abc123": {"views": 10, "privacy": "private"}
        }

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    with httpx.Client(transport=httpx.MockTransport(google_with_analytics(refused))) as http:
        with pytest.raises(ToolError) as signed_out:
            yt.outcomes_of(http, ENV, POSTS[:1], NOW)
    assert core_oauth.is_auth_failure(str(signed_out.value))


def test_the_channel_tool_names_the_channel_the_account_posts_to() -> None:
    seen: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"snippet": {"title": "Hooks Daily"}}]})

    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        assert yt.channel_name(http, ENV) == "Hooks Daily"
    (channels,) = seen
    assert channels.url.path == "/youtube/v3/channels"
    assert (channels.url.params["part"], channels.url.params["mine"]) == ("snippet", "true")
    assert channels.headers["authorization"] == "Bearer tok"

    def no_channel(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(no_channel)) as http:
        with pytest.raises(ToolError, match="no YouTube channel"):
            yt.channel_name(http, ENV)


# The Manifest, and the package installed as Swarmify does at startup


def test_the_manifest_declares_a_google_sign_in_with_todays_scopes() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["stage_types"]) == ("youtube", "publish", ["connector"])
    (account,) = m["config"]
    assert (account["key"], account["type"], account["env"]) == ("youtube", "oauth", yt.TOKEN)
    assert account["oauth"]["scopes"] == [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
    params = {"access_type": "offline", "prompt": "consent"}  # a refresh token, every sign-in
    assert account["oauth"]["authorize_params"] == params
    url = core_oauth.authorize_url(account, "id", "https://box.ts.net/cb", "st", "v" * 43)
    assert "access_type=offline" in url and "youtube.readonly" in url


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    """A fake Google sign-in: the core's token requests to Google reach it instead."""
    post = httpx.post

    def to_fake(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://oauth2.googleapis.com/token"
        return post(f"{fake.url}/token", **kwargs)

    with FakeProvider(client_id="yt-app", client_secret="yt-secret") as fake:
        monkeypatch.setattr(core_oauth.httpx, "post", to_fake)
        yield fake


@pytest.fixture
def v1(tmp_path: Path, provider: FakeProvider) -> Callable[[Any], Connectors]:
    """A V1 data directory whose .env holds YouTube's app and the refresh token V1's terminal
    `authorize` printed, upgraded: the default packages installed for real, youtube given to
    world `w`."""
    provider.refresh_tokens.add("rt-from-v1")
    env = tmp_path / ".env"
    env.write_text(
        "YOUTUBE_CLIENT_ID=yt-app\nYOUTUBE_CLIENT_SECRET=yt-secret\n"
        "YOUTUBE_REFRESH_TOKEN=rt-from-v1\n"
    )
    env.chmod(0o600)

    def make(transport: Any) -> Connectors:
        connectors = registry(tmp_path, transport)
        assert connectors.install_defaults()["youtube"] == "1.2.0"
        connectors.offer_defaults("w")
        return connectors

    return make


def test_a_v1_sign_in_is_carried_over_so_no_new_sign_in_is_needed(
    v1: Callable[[Any], Connectors], provider: FakeProvider, tmp_path: Path
) -> None:
    connectors = v1(FakeTransport())
    folder = tmp_path / "packages" / "youtube" / "1.2.0"
    assert (folder / ".venv" / "bin" / "python").is_file() and (folder / "youtube.py").is_file()
    shared, _ = connectors.setups.values("youtube", "w")
    assert (shared["youtube_client_id"], shared["youtube_client_secret"]) == ("yt-app", "yt-secret")
    assert shared["youtube"]["refresh_token"] == "rt-from-v1"
    youtube = connectors.find("w", "youtube")
    assert youtube is not None and youtube.version == "1.2.0"
    assert connectors.missing(youtube) == [] and connectors.sign_in_needed(youtube) == []

    env = connectors._env(youtube)  # the first start refreshes the V1 token
    assert provider.grants == ["refresh_token"]
    assert env == {"YOUTUBE_ACCESS_TOKEN": next(iter(provider.access_tokens))}
    assert connectors._env(youtube) == env and provider.grants == ["refresh_token"]  # kept


def test_the_installed_package_previews_and_publishes_over_stdio(
    v1: Callable[[Any], Connectors], tmp_path: Path
) -> None:
    connectors = v1(McpTransport())
    config = stage(hashtags="#owls", tags="owls, owl facts", schedule="2099-01-01T09:00:00Z")
    shown = connectors.publish_preview("w", config, {"description": "Why owls turn."})
    assert shown is not None and (shown["connector"], shown["problems"]) == ("youtube", [])
    assert values(shown) == {
        "title": "Five hooks",
        "description": "Why owls turn.",
        "hashtags": "#owls",
        "tags": "owls, owl facts",
        "visibility": "public",
        "made_for_kids": False,
        "schedule": "2099-01-01T09:00:00Z",
        "video": "short.mp4",
    }
    # a saved stage is checked as its gate will be, through the same tool
    assert connectors.stage_problems("w", stage(title="", visibility="secret")) == [
        "a publish stage needs params.title",
        "visibility is one of public, unlisted, private, got 'secret'",
    ]

    gone = str(tmp_path / "gone.mp4")  # fails before reaching Google
    sent = {**config, "params": {**config["params"], "input_path": gone}}
    with pytest.raises(ConnectorError, match="gone.mp4 does not exist"):
        connectors.run_stage("w", "run_1", "post", {**sent, "confirmed": values(shown)})
    call = connectors.call_for("post")
    assert call is not None and call.tool == "publish"
    assert call.request == {"fields": values(shown), "idempotency_key": "post", "input_path": gone}
