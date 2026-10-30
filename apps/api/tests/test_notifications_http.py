"""Slack notifications over HTTP: the settings page's webhook per world, and real runs on DBOS
posting gate opened, run done and run failed to a fake Slack endpoint with deep links."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from api_fakes import studio, until
from fastapi.testclient import TestClient

from swarmify_api.main import create_app
from swarmify_notifications.fake import FakeSlack

BASE = "http://swarmify.lan:8000"
QUICK = "name: Quick\nstages:\n  - gate: ok\n    ask: Go?\n    kind: confirm\n"
BOOM = "name: Boom\nstages:\n  - connector: explode\n    calls: studio\n"


@pytest.fixture
def slack() -> Iterator[FakeSlack]:
    with FakeSlack() as fake:
        yield fake


@pytest.fixture
def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with its default Notifier, the Slack adapter, and a connector that fails on
    a stage named explode."""
    monkeypatch.setenv("SWARMIFY_BASE_URL", f"{BASE}/")

    def connector(_run: object, _id: str, stage: object) -> None:
        if getattr(stage, "name", "") == "explode":
            raise RuntimeError("connector said no")

    with TestClient(create_app(tmp_path, connector=connector, retro=False)) as client:
        yield client


def world(client: TestClient, name: str = "Video") -> str:
    world_id = client.post("/api/worlds", json={"name": name}).json()["id"]
    studio(client, world_id)
    return world_id


def start(client: TestClient, world_id: str, yaml: str) -> str:
    wf = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": yaml}).json()
    return client.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()["id"]


def test_the_settings_page_stores_a_webhook_per_world_and_never_returns_it(
    running: TestClient, slack: FakeSlack
) -> None:
    video, other = world(running), world(running, "Markets")
    url = f"/api/worlds/{video}/notifications"
    assert running.get(url).json() == {"slack_webhook": None}
    assert running.post(f"{url}/test").status_code == 409

    saved = running.put(url, json={"slack_webhook": slack.url})
    assert saved.status_code == 200
    shown = f"{slack.url.split('/services')[0]}/...B0/x"
    assert saved.json() == running.get(url).json() == {"slack_webhook": shown}
    assert running.get(f"/api/worlds/{other}/notifications").json() == {"slack_webhook": None}

    assert running.post(f"{url}/test").status_code == 204
    assert slack.texts() == [f"Swarmify is connected. <{BASE}/gates|Open gates>"]
    slack.status = 404
    refused = running.post(f"{url}/test")
    assert refused.status_code == 502 and "Slack did not take" in refused.json()["detail"]

    bad = running.put(url, json={"slack_webhook": "file:///etc/passwd"})
    assert bad.status_code == 422 and "https URL" in bad.json()["detail"]
    assert running.put(url, json={"slack_webhook": ""}).json() == {"slack_webhook": None}
    assert running.get("/api/worlds/world_nope/notifications").status_code == 404
    assert running.put("/api/worlds/world_nope/notifications", json={}).status_code == 404


def test_gate_opened_run_done_and_run_failed_post_to_slack_with_deep_links(
    running: TestClient, slack: FakeSlack
) -> None:
    video = world(running)
    running.put(f"/api/worlds/{video}/notifications", json={"slack_webhook": slack.url})
    quiet = world(running, "Quiet")  # no webhook: its runs post nothing

    run = start(running, video, QUICK)
    until(lambda: len(slack.posts) == 1)
    assert slack.texts() == [
        f"*Quick* is waiting on you at gate ok. Go? <{BASE}/gates|Answer the gate>"
    ]
    assert running.post(f"/api/runs/{run}/decision", json={}).status_code == 200
    until(lambda: len(slack.posts) == 2)
    assert slack.texts()[1] == f"*Quick* is done. <{BASE}/worlds/{video}/runs/{run}|Open the run>"

    start(running, quiet, BOOM)
    failed = start(running, video, BOOM)
    until(lambda: running.get(f"/api/runs/{failed}").json()["status"] == "failed")
    until(lambda: len(slack.posts) == 3)
    assert slack.texts()[2] == (
        f"*Boom* failed. <{BASE}/worlds/{video}/runs/{failed}|Open the run>"
    )


def test_a_slack_outage_does_not_fail_the_run_and_deleting_the_world_drops_its_channel(
    running: TestClient, slack: FakeSlack, tmp_path: Path
) -> None:
    video = world(running)
    running.put(f"/api/worlds/{video}/notifications", json={"slack_webhook": slack.url})
    slack.status = 500
    run = start(running, video, QUICK)
    until(lambda: running.get(f"/api/runs/{run}").json()["status"] == "waiting_gate")
    running.post(f"/api/runs/{run}/decision", json={})
    until(lambda: running.get(f"/api/runs/{run}").json()["status"] == "done")
    until(lambda: len(slack.posts) == 2)  # both were tried; run.ended posts after done is saved

    assert running.delete(f"/api/worlds/{video}").status_code == 204
    with closing(sqlite3.connect(tmp_path / "swarmify.db")) as conn:
        assert conn.execute("SELECT count(*) FROM channels").fetchone() == (0,)


def test_aborting_a_queued_run_during_a_slack_outage_still_aborts_it(
    running: TestClient, slack: FakeSlack
) -> None:
    video = world(running)
    running.put(f"/api/worlds/{video}/notifications", json={"slack_webhook": slack.url})
    slack.status = 500
    waiting = [start(running, video, QUICK) for _ in range(2)]  # DEFAULT_CAP is 2
    for run in waiting:
        until(lambda run=run: running.get(f"/api/runs/{run}").json()["status"] == "waiting_gate")
    queued = start(running, video, QUICK)
    until(lambda: len(slack.posts) == 2)
    assert running.get(f"/api/runs/{queued}").json()["status"] == "queued"

    aborted = running.post(f"/api/runs/{queued}/abort")
    assert aborted.status_code == 200
    assert running.get(f"/api/runs/{queued}").json()["status"] == "aborted"
    assert len(slack.posts) == 3, "the run.ended post was tried"
