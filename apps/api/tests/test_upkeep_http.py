"""Scheduled upkeep through the API: view counts written back into publish artifacts and old run
binaries pruned past the world's retention, markdown kept. The fake ConnectorTransport answers
for YouTube; DBOS runs for real on SQLite; the upkeep interval is shortened."""

import os
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_publish_http import KEYS, at_gate, setup, until, weekly

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


def test_view_counts_are_written_into_the_publish_artifact_body(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    running.put("/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS})
    world, workflow = setup(running, weekly())
    run = at_gate(running, tmp_path, world, workflow)
    transport.outputs["youtube"] = [
        {"video_id": "vid1", "url": "https://youtu.be/vid1", "publish_at": "2099-01-01T09:00:00Z"}
    ]
    transport.outputs["views"] = [{"views": {"vid1": 9000}}]
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    (publish,) = (tmp_path / "worlds" / world / "runs" / run["id"]).glob("*/artifacts/publish.md")

    until(lambda: "## Outcome" in publish.read_text())
    text = publish.read_text()
    assert (
        "\nviews: 9000\n" in text and "9,000 views as of " in text and "- vid1: 9,000 views" in text
    )
    assert ("youtube", "views", {"video_ids": ["vid1"]}, KEYS) in transport.calls
    stage = next(
        s for s in running.get(f"/api/runs/{run['id']}").json()["stages"] if s["name"] == "publish"
    )
    (card,) = running.get(f"/api/runs/{run['id']}/stages/{stage['id']}/artifacts").json()
    assert card["type"] == "publish" and card["fields"]["views"] == 9000

    seen = len(transport.calls)
    until(lambda: len(transport.calls) > seen + 2)  # later ticks read the count again
    assert publish.read_text() == text  # an unchanged count is not written again
    transport.outputs["views"] = [{"views": {"vid1": 41000}}]
    until(lambda: "41,000 views" in publish.read_text())
    assert publish.read_text().count("## Outcome") == 1


def test_a_world_without_youtube_keys_is_skipped(
    running: TestClient, transport: FakeTransport, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = running.post("/api/worlds", json={"name": "Video"}).json()["id"]
    folder = tmp_path / "worlds" / world / "runs/run_1/03-publish/artifacts"
    folder.mkdir(parents=True)
    body = '---\ntype: "publish"\nconnector: "youtube"\nid: "vid1"\n---\n\n# Five\n'
    (folder / "publish.md").write_text(body)
    until(lambda: f"view counts for world {world} skipped" in caplog.text)  # a tick saw it
    assert not any(c[1] == "views" for c in transport.calls)
    assert (folder / "publish.md").read_text() == body


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
