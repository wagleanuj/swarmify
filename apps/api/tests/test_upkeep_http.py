"""Scheduled upkeep through the API: Outcomes from every publish connector offering them recorded
beside publish artifacts (never in them: the graph reads those) and onto the scorecard, and old
run binaries pruned past the world's retention, markdown kept. The fake ConnectorTransport
answers for the connectors, YouTube's `outcomes` claiming posts as its package does; DBOS runs
for real on SQLite; the upkeep interval is shortened."""

import json
import os
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_fakes import YOUTUBE_ENV, install, sign_in_youtube, youtube, youtube_outcomes
from fastapi.testclient import TestClient
from test_publish_http import MAKER, at_gate, setup, until, weekly

from fake_graph import fake_extract
from swarmify_api import upkeep
from swarmify_api.main import create_app
from swarmify_connectors import FakeTransport
from swarmify_connectors.setup import SetupStore
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_memory import FakeGraphIndex
from swarmify_swarm import SdkRuntime
from swarmify_swarm.fake import Script


@pytest.fixture
def running(tmp_path: Path, transport: FakeTransport) -> Iterator[TestClient]:
    app = create_app(
        tmp_path,
        transport=transport,
        poll_seconds=0.01,
        runtime=SdkRuntime(transport=Script().transport),
        graph_index=FakeGraphIndex(),
        extract=fake_extract,
        upkeep_seconds=0.05,
        retro=False,
    )
    with TestClient(app) as client:
        yield client


def _types(root: Path, world: str) -> list[str]:
    with closing(connect(root / "swarmify.db")) as conn:
        return [e.type for e in read_after(conn, world, 0)]


def test_youtube_counts_the_views_of_a_video_upload_post_put_on_youtube(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    sign_in_youtube(tmp_path)
    world, workflow = setup(running, weekly("upload_post", "\n      user: me"))
    keys = {"shared": {"api_key": "k"}}
    assert running.put(f"/api/worlds/{world}/packages/upload_post/setup", json=keys).is_success
    run = at_gate(running, tmp_path, world, workflow)
    links = {"youtube": "https://youtu.be/vid123"}
    transport.outputs["publish"] = [{"id": "job_1", "links": links}]
    transport.outputs["youtube.outcomes"] = [youtube_outcomes(9000)]
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    (publish,) = (tmp_path / "worlds" / world / "runs" / run["id"]).glob("*/artifacts/publish.md")

    text = publish.read_text()
    outcomes = publish.with_name(".publish.md.outcomes.json")
    # the sidecar is written before the event, so the event means both are in place
    until(lambda: "outcome.recorded" in _types(tmp_path, world))
    assert json.loads(outcomes.read_text())["outcomes"] == {"views": 9000}
    assert json.loads(outcomes.read_text())["primary_metric"] == "views"
    posts = [{"connector": "upload_post", "id": "job_1", "links": links}]
    assert ("youtube", "outcomes", {"posts": posts}, YOUTUBE_ENV) in transport.calls
    stage = next(
        s for s in running.get(f"/api/runs/{run['id']}").json()["stages"] if s["name"] == "publish"
    )
    (card,) = running.get(f"/api/runs/{run['id']}/stages/{stage['id']}/artifacts").json()
    assert card["type"] == "publish" and card["fields"]["outcomes"] == {"views": 9000}

    seen, written = len(transport.calls), outcomes.stat().st_mtime_ns
    until(lambda: len(transport.calls) > seen + 2)  # later ticks read the count again
    assert outcomes.stat().st_mtime_ns == written  # an unchanged count is not written again
    transport.outputs["youtube.outcomes"] = [youtube_outcomes(41000)]
    until(lambda: '"views": 41000' in outcomes.read_text())
    assert publish.read_text() == text  # the record the graph reads never changes for a count


CLICKS = "packages/connectors/tests/fixtures/packages/clicks"


def test_a_connector_reporting_clicks_shows_in_the_record_and_the_scorecard(
    running: TestClient, transport: FakeTransport, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert install(running, CLICKS).status_code == 201
    world = running.post("/api/worlds", json={"name": "Links"}).json()["id"]
    assert running.put(f"/api/worlds/{world}/packages/clicks", json={"version": "1.0.0"}).is_success
    keys = {"shared": {"token": "t"}}
    assert running.put(f"/api/worlds/{world}/packages/clicks/setup", json=keys).is_success
    assert running.post(f"/api/worlds/{world}/connectors", json=MAKER).status_code == 201
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": weekly("clicks")})
    workflow = saved.json()["workflow_id"]
    run = at_gate(running, tmp_path, world, workflow)
    transport.outputs["publish"] = [{"id": "p1", "url": "https://sho.rt/p1"}]
    transport.outputs["clicks.outcomes"] = [
        {"p1": {"clicks": 12, "views": 300}, "other": {"clicks": 1}}
    ]
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    (publish,) = (tmp_path / "worlds" / world / "runs" / run["id"]).glob("*/artifacts/publish.md")

    outcomes = publish.with_name(".publish.md.outcomes.json")
    # the sidecar is written before the event, so the event means both are in place
    until(lambda: "outcome.recorded" in _types(tmp_path, world))
    found = json.loads(outcomes.read_text())
    assert found["outcomes"] == {"clicks": 12, "views": 300}
    assert found["primary_metric"] == "clicks"
    posts = [{"connector": "clicks", "id": "p1", "links": {"clicks": "https://sho.rt/p1"}}]
    assert ("clicks", "outcomes", {"posts": posts}, {"CLICKS_TOKEN": "t"}) in transport.calls
    # youtube offers outcomes too, but is not signed in here: skipped, the others still asked
    assert f"outcomes from youtube for world {world} skipped" in caplog.text
    assert not any(c[:2] == ("youtube", "outcomes") for c in transport.calls)

    (score,) = running.get(f"/api/workflows/{workflow}/scorecard").json()["runs"]
    assert score["outcomes"] == {"clicks": 12}  # ranked by its Primary Metric


def test_retention_deletes_old_binaries_and_keeps_their_markdown(
    running: TestClient, tmp_path: Path
) -> None:
    video = running.post("/api/worlds", json={"name": "Video"}).json()
    keep = running.post("/api/worlds", json={"name": "Archive"}).json()
    assert video["retention_days"] == 90
    changed = running.patch(f"/api/worlds/{video['id']}", json={"retention_days": 30})
    assert changed.status_code == 200 and changed.json()["retention_days"] == 30
    assert running.patch(f"/api/worlds/{keep['id']}", json={"retention_days": 0}).status_code == 200

    old = datetime.now(UTC) - timedelta(days=31)
    files = {}
    for world in (video["id"], keep["id"]):
        stage = tmp_path / "worlds" / world / "runs/run_1/01-render"
        (stage / "artifacts").mkdir(parents=True)
        files[world] = [
            stage / "artifacts/short.mp4",
            stage / "artifacts/short.mp4.md",
            stage / "artifacts/script.md",
            stage / "artifacts/fresh.mp4",
        ]
        for f in files[world]:
            f.write_text("x")
        for f in files[world][:3]:
            os.utime(f, (old.timestamp(), old.timestamp()))

    short, sidecar, script, fresh = files[video["id"]]
    # The sweep appends the event just after the unlink, so wait for the event, not the file.
    until(lambda: "artifact.pruned" in _types(tmp_path, video["id"]))
    assert not short.exists()
    assert sidecar.exists() and script.exists() and fresh.exists()
    assert "short.mp4 was deleted by retention" in sidecar.read_text()
    assert all(f.exists() for f in files[keep["id"]])  # 0 keeps files forever


UNSAID = """name: Daily short
stages:
  - connector: render
    calls: maker
    output: short.mp4
  - gate: confirm
    ask: Post it?
    kind: confirm
  - connector: publish
    calls: youtube
    input: short.mp4
    params:
      title: Five hooks that work
"""


@pytest.fixture
def two_days_on(tmp_path: Path, transport: FakeTransport) -> Iterator[TestClient]:
    """The app with its clock two days ahead, so a post just made reads as a day old and more."""
    app = create_app(
        tmp_path,
        transport=transport,
        poll_seconds=0.01,
        runtime=SdkRuntime(transport=Script().transport),
        graph_index=FakeGraphIndex(),
        extract=fake_extract,
        upkeep_seconds=0.05,
        retro=False,
        clock=lambda: datetime.now(UTC) + timedelta(days=2),
    )
    with TestClient(app) as client:
        yield client


def test_a_post_still_private_and_unread_retention_and_a_refused_sign_in_reach_needs_you(
    two_days_on: TestClient,
    transport: FakeTransport,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    running = two_days_on
    sign_in_youtube(tmp_path)
    world, workflow = setup(running, UNSAID)
    run = at_gate(running, tmp_path, world, workflow)
    transport.outputs["publish"] = [{"id": "vid1", "url": "https://youtu.be/vid1"}]
    retention = {"youtube_avg_viewed_percent": 55.5, "youtube_avg_view_seconds": 9.25}

    def outcomes(args: dict) -> dict:
        found = {
            p: {"views": 3, "visible": False, "visibility_label": "private", **retention}
            for p in youtube.claimed(args["posts"])
        }
        return {**found, youtube.NOTICE: youtube.SCOPE_NOTICE}

    transport.outputs["youtube.outcomes"] = [outcomes]
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    (publish,) = (tmp_path / "worlds" / world / "runs" / run["id"]).glob("*/artifacts/publish.md")
    assert "visibility_said: false\n" in publish.read_text()  # the stage never said one
    sidecar = publish.with_name(".publish.md.outcomes.json")
    until(lambda: sidecar.is_file())
    found = json.loads(sidecar.read_text())
    assert found["outcomes"] == {"views": 3, **retention}
    assert (found["visible"], found["visibility_label"]) == (False, "private")

    seen = len(transport.calls)
    until(lambda: len(transport.calls) > seen + 2)  # several upkeep passes
    notices = running.get("/api/notices").json()
    by_kind = {n["kind"]: n for n in notices}
    hidden = by_kind["visibility"]
    assert (hidden["world_id"], hidden["run_id"]) == (world, run["id"])
    # in the connector's own label and word, never core's
    assert hidden["text"] == (
        "Five hooks that work is still private on YouTube a day on, and its stage never said"
        " who sees it, so YouTube's default chose."
    )
    assert by_kind["outcomes"]["text"] == youtube.SCOPE_NOTICE
    assert "sign_in" not in by_kind
    # said once in the log, not on every pass
    assert caplog.text.count("Sign in to YouTube again") == 1

    # Google refuses the refresh token (invalid_grant): the account is marked for sign-in
    store = SetupStore(tmp_path / "setup.json")
    tokens = {"access_token": "at", "refresh_token": "rt", "expires_at": 0, "needs_sign_in": True}
    store.put_value("youtube", None, "youtube", tokens)
    running.post("/api/worlds", json={"name": "Another"})  # a shared account: one notice
    signed_out = [n for n in running.get("/api/notices").json() if n["kind"] == "sign_in"]
    assert len(signed_out) == 1
    assert signed_out[0]["text"].startswith("YouTube needs sign-in")


def test_a_post_its_stage_kept_private_or_scheduled_for_later_is_no_notice() -> None:
    now = datetime(2026, 11, 11, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()

    def record(**fields: object) -> tuple[str, dict]:
        base = {"visible": False, "created": old, "run": "r1", "connector": "poster"}
        return ("p.md", {**base, "title": "T", "visibility_said": True, **fields})

    def status(run: str) -> str:
        return {"r1": "done", "r2": "running"}[run]

    records = [
        record(public=False),  # meant
        record(public=True, visible=True),  # shown as meant
        record(public=True, visible=None),  # its connector never says
        record(public=True, created=now.isoformat()),  # not a day yet
        record(public=True, scheduled_for="2026-11-20T09:00:00Z"),
        record(visibility_said=False, run="r2"),  # unsaid, but its run is still going
        record(public=True, visibility_label="draft"),
        record(visibility_said=False),
        # Legacy (youtube 1.2.0): the stage's own word and the platform's, read as before
        record(visibility_said=None, visible=None, visibility="private", privacy="private"),
        record(
            visibility_said=None,
            visibility="public",
            sent=[{"key": "schedule", "value": "2026-11-20T09:00:00Z"}],
        ),
        record(visibility_said=None, visible=None, visibility="public", privacy="unlisted"),
        record(visibility_said=None, visible=None, privacy="private", title="Old"),
    ]
    found = upkeep.hidden_posts(records, status, now, {"poster": "Poster"})
    assert [why for _, why in found] == [
        "T is still draft on Poster a day on, though it went out for everyone to see.",
        "T is still not public on Poster a day on, and its stage never said who sees it,"
        " so Poster's default chose.",
        "T is still unlisted on Poster a day on, though it went out for everyone to see.",
        "Old is still private on Poster a day on, and its stage never said who sees it,"
        " so Poster's default chose.",
    ]
