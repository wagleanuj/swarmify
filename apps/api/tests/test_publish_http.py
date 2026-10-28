"""Publishing over HTTP (ADR 0007): keys for the built-in publish connectors, the confirm gate
that shows exactly what goes out, the call that sends it, the publish artifact with its link,
and the editor and runner refusing a publish stage no gate precedes. The fake ConnectorTransport
stands in for YouTube and Upload-Post; DBOS runs for real on SQLite."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import FakeTransport

KEYS = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}
MAKER = {
    "name": "maker",
    "kind": "stage",
    "server": "uvx maker-mcp",
    "credentials": {},
    "stage_types": ["connector"],
}


def weekly(calls: str = "youtube", extra: str = "") -> str:
    return f"""name: Weekly short
stages:
  - connector: render
    calls: maker
    output: short.mp4
  - gate: confirm
    ask: Post it?
    kind: confirm
  - connector: publish
    calls: {calls}
    input: short.mp4
    params:
      title: Five hooks that work
      visibility: public
      schedule: "2099-01-01T10:00:00+01:00"{extra}
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def setup(client: TestClient, yaml: str) -> tuple[str, str]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=MAKER).status_code == 201
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 201, saved.text
    return world, saved.json()["workflow_id"]


def at_gate(client: TestClient, tmp_path: Path, world: str, workflow: str) -> dict:
    run = client.post(f"/api/workflows/{workflow}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    # What the render stage would have left in the run folder.
    out = tmp_path / "worlds" / world / "runs" / run["id"] / "01-render" / "short.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"video")
    return client.get(f"/api/runs/{run['id']}").json()


def test_built_in_publish_connectors_take_their_keys_and_never_return_them(
    client: TestClient, tmp_path: Path
) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    listed = {c["name"]: c for c in client.get(f"/api/worlds/{world}/connectors").json()}
    assert listed["youtube"]["missing"] == list(KEYS)
    assert listed["upload_post"]["missing"] == ["UPLOAD_POST_API_KEY"]
    assert listed["upload_post"]["command"][:2] == ["npx", "-y"]

    url = "/api/connectors/builtin_youtube/credentials"
    saved = client.put(url, json={"credentials": KEYS})
    assert saved.status_code == 200 and saved.json()["missing"] == []
    assert set(saved.json()) == set(listed["youtube"])  # names and status, never values
    assert "YOUTUBE_REFRESH_TOKEN=r" in (tmp_path / ".env").read_text()
    foreign = client.put(url, json={"credentials": {"FAL_KEY": "k"}})
    assert foreign.status_code == 422 and "does not use FAL_KEY" in foreign.json()["detail"]
    unknown = client.put("/api/connectors/nope/credentials", json={"credentials": KEYS})
    assert unknown.status_code == 404


def test_the_confirm_gate_shows_what_goes_out_and_confirming_posts_exactly_that(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    running.put("/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS})
    world, workflow = setup(running, weekly())
    run = at_gate(running, tmp_path, world, workflow)

    (gate,) = running.get("/api/gates").json()
    assert gate["kind"] == "confirm"
    assert gate["publish"] == {
        "connector": "youtube",
        "title": "Five hooks that work",
        "visibility": "public",
        "schedule": "2099-01-01T09:00:00Z",
        "description": "",
        "video": "short.mp4",
        "platforms": [],
        "made_for_kids": False,
        "problems": [],
    }
    assert [c[0] for c in transport.calls] == ["maker"]

    transport.outputs["youtube"] = [
        {"video_id": "vid1", "url": "https://youtu.be/vid1", "publish_at": "2099-01-01T09:00:00Z"}
    ]
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")

    name, tool, args, env = transport.calls[-1]
    assert (name, tool, env) == ("youtube", "youtube", KEYS)
    assert (args["title"], args["visibility"], args["schedule"]) == (
        "Five hooks that work",
        "public",
        "2099-01-01T09:00:00Z",
    )
    assert args["input_path"].endswith(f"{run['id']}/01-render/short.mp4")

    stage_id = run["stages"][2]["id"]
    artifact = tmp_path / "worlds" / world / "runs" / run["id"] / stage_id / "artifacts"
    text = (artifact / "publish.md").read_text()
    assert text.startswith('---\ntype: "publish"\n')
    assert 'links: {"youtube": "https://youtu.be/vid1"}\n' in text
    assert 'status: "scheduled"\n' in text and 'id: "vid1"\n' in text
    assert "private until 2099-01-01T09:00:00Z, then public" in text
    assert "- youtube: https://youtu.be/vid1\n" in text


def test_upload_post_schedules_through_its_own_tool(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    keys = {"credentials": {"UPLOAD_POST_API_KEY": "k"}}
    running.put("/api/connectors/builtin_upload_post/credentials", json=keys)
    extra = "\n      user: me\n      platforms: [tiktok, instagram]"
    world, workflow = setup(running, weekly("upload_post", extra))
    run = at_gate(running, tmp_path, world, workflow)
    assert running.get("/api/gates").json()[0]["publish"]["platforms"] == ["tiktok", "instagram"]

    transport.outputs["upload_video"] = [{"result": {"success": True, "job_id": "job_9"}}]
    running.post(f"/api/runs/{run['id']}/decision", json={})
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    _, tool, args, _ = transport.calls[-1]
    assert tool == "upload_video" and args["scheduledDate"] == "2099-01-01T09:00:00Z"
    assert args["platforms"] == ["tiktok", "instagram"] and args["user"] == "me"
    stage_id = run["stages"][2]["id"]
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / stage_id / "artifacts"
    text = (folder / "publish.md").read_text()
    assert 'status: "scheduled"\n' in text and "upload_post job job_9" in text


def test_a_gate_shows_the_problems_that_would_stop_the_post(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    world, workflow = setup(running, weekly("upload_post"))
    run = at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert any("needs params.user" in p for p in shown["problems"])
    running.post(f"/api/runs/{run['id']}/decision", json={})
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "failed")
    failed = running.get(f"/api/runs/{run['id']}").json()["stages"][2]
    assert "needs params.user" in failed["error"]
    assert [c[0] for c in transport.calls] == ["maker"]


SCRIPT = """---
title: "Why owls turn their heads"
description: |
  Owls cannot move their eyes.
  So they turn their heads.
---

# Script
"""


def test_title_and_description_come_from_an_earlier_artifact_and_the_gate_edits_them(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    running.put("/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS})
    extra = "\n      metadata: script.md\n      description: fallback\n      made_for_kids: true"
    world, workflow = setup(
        running, weekly(extra=extra).replace("2099-01-01T10:00:00+01:00", "+1d 09:00")
    )
    run = at_gate(running, tmp_path, world, workflow)
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / "01-render"
    (folder / "script.md").write_text(SCRIPT)

    shown = running.get("/api/gates").json()[0]["publish"]
    assert shown["title"] == "Why owls turn their heads"
    assert shown["description"] == "Owls cannot move their eyes.\nSo they turn their heads."
    assert shown["made_for_kids"] is True and shown["problems"] == []
    assert shown["schedule"].endswith("T09:00:00Z")

    url = f"/api/runs/{run['id']}/decision"
    refused = running.post(url, json={"title": "x" * 101})
    assert refused.status_code == 422 and "the limit is 100" in refused.json()["detail"]
    transport.outputs["youtube"] = [{"video_id": "v", "url": "https://youtu.be/v"}]
    edited = {"title": "Owls, explained", "description": "Edited."}
    assert running.post(url, json=edited).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")

    _, _, args, _ = transport.calls[-1]
    assert (args["title"], args["description"]) == ("Owls, explained", "Edited.")
    assert (args["schedule"], args["made_for_kids"]) == (shown["schedule"], True)
    (decision,) = running.get(f"/api/runs/{run['id']}").json()["decisions"]
    assert decision["publish"] == {**edited, "schedule": shown["schedule"]}
    stage_id = run["stages"][2]["id"]
    artifact = tmp_path / "worlds" / world / "runs" / run["id"] / stage_id / "artifacts"
    assert (
        (artifact / "publish.md")
        .read_text()
        .split("\n---\n", 1)[1]
        .startswith("\n# Owls, explained")
    )


def test_a_missing_metadata_artifact_is_a_problem_the_gate_shows(
    running: TestClient, tmp_path: Path
) -> None:
    world, workflow = setup(running, weekly(extra="\n      metadata: script.md"))
    at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert shown["title"] == "Five hooks that work"
    assert any("script.md" in p for p in shown["problems"])


@pytest.mark.parametrize("calls", ["youtube", "upload_post"])
def test_the_editor_refuses_a_publish_stage_without_a_gate(client: TestClient, calls: str) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    yaml = f"name: Sneaky\nstages:\n  - connector: publish\n    calls: {calls}\n"
    checked = client.post(f"/api/worlds/{world}/workflows/check", json={"yaml": yaml}).json()
    assert [v["rule"] for v in checked["violations"]] == ["publish_without_gate"]
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 422


def test_the_runner_refuses_a_connector_that_turned_publish_after_the_save(
    running: TestClient, transport: FakeTransport
) -> None:
    yaml = "name: Sneaky\nstages:\n  - connector: post\n    calls: poster\n"
    poster = {**MAKER, "name": "poster"}
    world, workflow = setup(running, "name: x\nstages:\n  - connector: m\n    calls: maker\n")
    added = running.post(f"/api/worlds/{world}/connectors", json=poster).json()
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
    running.delete(f"/api/connectors/{added['id']}")
    publishing = {**poster, "kind": "publish"}
    assert running.post(f"/api/worlds/{world}/connectors", json=publishing).status_code == 201

    run = running.post(f"/api/workflows/{saved['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "failed")
    error = running.get(f"/api/runs/{run['id']}").json()["stages"][0]["error"]
    assert "calls a publish connector, so it runs only right after an answered gate" in error
    assert transport.calls == []
