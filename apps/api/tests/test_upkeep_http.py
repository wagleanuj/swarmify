"""Scheduled upkeep through the API: Outcomes from every publish connector offering them written
back into publish artifacts and onto the scorecard, and old run binaries pruned past the world's
retention, markdown kept. The fake ConnectorTransport answers for the connectors, YouTube's
`outcomes` claiming posts as its package does; DBOS runs for real on SQLite; the upkeep interval
is shortened."""

import os
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_fakes import YOUTUBE_ENV, install, sign_in_youtube, youtube_outcomes
from fastapi.testclient import TestClient
from test_publish_http import MAKER, at_gate, setup, until, weekly

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_connectors import FakeTransport
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

    until(lambda: "## Outcome" in publish.read_text())
    text = publish.read_text()
    assert '\noutcomes: {"views": 9000}\n' in text and 'primary_metric: "views"' in text
    assert "## Outcome\n\n9,000 views as of " in text
    posts = [{"connector": "upload_post", "id": "job_1", "links": links}]
    assert ("youtube", "outcomes", {"posts": posts}, YOUTUBE_ENV) in transport.calls
    stage = next(
        s for s in running.get(f"/api/runs/{run['id']}").json()["stages"] if s["name"] == "publish"
    )
    (card,) = running.get(f"/api/runs/{run['id']}/stages/{stage['id']}/artifacts").json()
    assert card["type"] == "publish" and card["fields"]["outcomes"] == {"views": 9000}

    seen = len(transport.calls)
    until(lambda: len(transport.calls) > seen + 2)  # later ticks read the count again
    assert publish.read_text() == text  # an unchanged count is not written again
    transport.outputs["youtube.outcomes"] = [youtube_outcomes(41000)]
    until(lambda: "41,000 views" in publish.read_text())
    assert publish.read_text().count("## Outcome") == 1


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

    until(lambda: "## Outcome" in publish.read_text())
    text = publish.read_text()
    assert '\noutcomes: {"clicks": 12, "views": 300}\n' in text
    assert "12 clicks, 300 views as of " in text and 'primary_metric: "clicks"' in text
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
