"""Publishing over HTTP (ADR 0007): keys for the built-in publish connectors, the confirm gate
that shows exactly what goes out, the call that sends it, the publish artifact with its link,
and the editor and runner refusing a publish stage no gate precedes. The fake ConnectorTransport
stands in for YouTube and Upload-Post; DBOS runs for real on SQLite."""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import ConnectorError, FakeTransport

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


def vals(shown: dict) -> dict:
    """A gate's publish preview as its values by key."""
    return {f["key"]: f["value"] for f in shown["fields"]}


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
    # a default package now, whose key is a Setup field (connectors/upload_post)
    assert (listed["upload_post"]["missing"], listed["upload_post"]["builtin"]) == (
        ["api_key"],
        False,
    )

    url = "/api/connectors/builtin_youtube/credentials"
    saved = client.put(url, json={"credentials": KEYS})
    assert saved.status_code == 200 and saved.json()["missing"] == []
    assert set(saved.json()) == {*listed["youtube"], "check"}  # names and status, never values
    assert "YOUTUBE_REFRESH_TOKEN=r" in (tmp_path / ".env").read_text()
    foreign = client.put(url, json={"credentials": {"FAL_KEY": "k"}})
    assert foreign.status_code == 422 and "does not use FAL_KEY" in foreign.json()["detail"]
    unknown = client.put("/api/connectors/nope/credentials", json={"credentials": KEYS})
    assert unknown.status_code == 404


def test_saving_a_builtins_keys_answers_its_live_check_and_a_failure_still_saves(
    client: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    url = "/api/connectors/builtin_youtube/credentials"
    transport.outputs["channel"] = [{"channel": "Hooks Daily"}]
    saved = client.put(url, json={"credentials": KEYS}).json()
    assert saved["check"] == {"ok": True, "message": "Posts to the YouTube channel Hooks Daily."}
    transport.outputs["channel"] = [ConnectorError("refreshing the YouTube token failed (400)")]
    bad = {**KEYS, "YOUTUBE_REFRESH_TOKEN": "stale"}
    saved = client.put(url, json={"credentials": bad}).json()
    assert saved["check"] == {"ok": False, "message": "refreshing the YouTube token failed (400)"}
    assert "YOUTUBE_REFRESH_TOKEN=stale" in (tmp_path / ".env").read_text()


def test_the_confirm_gate_shows_what_goes_out_and_confirming_posts_exactly_that(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    running.put("/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS})
    world, workflow = setup(running, weekly())
    run = at_gate(running, tmp_path, world, workflow)

    (gate,) = running.get("/api/gates").json()
    assert gate["kind"] == "confirm"
    assert (gate["publish"]["connector"], gate["publish"]["problems"]) == ("youtube", [])
    assert vals(gate["publish"]) == {
        "title": "Five hooks that work",
        "description": "",
        "hashtags": "",
        "tags": "",
        "visibility": "public",
        "made_for_kids": False,
        "schedule": "2099-01-01T09:00:00Z",
        "video": "short.mp4",
    }
    assert [f["key"] for f in gate["publish"]["fields"] if f["editable"]] == [
        "title",
        "description",
        "hashtags",
        "tags",
    ]
    assert [c[1] for c in transport.calls] == ["channel", "maker"]  # the key check, the render

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
    assert "- When: 2099-01-01T09:00:00Z\n" in text and "- Visibility: public\n" in text
    assert "- youtube: https://youtu.be/vid1\n" in text


def test_upload_post_previews_and_schedules_through_its_package(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    extra = "\n      user: me\n      platforms: [tiktok, instagram]"
    world, workflow = setup(running, weekly("upload_post", extra))
    keys = {"shared": {"api_key": "k"}}
    assert running.put(f"/api/worlds/{world}/packages/upload_post/setup", json=keys).is_success
    platforms = {"key": "platforms", "label": "Platforms", "value": ["tiktok", "instagram"]}
    title = {"key": "title", "label": "Title", "value": "Five hooks that work", "editable": True}
    transport.outputs["preview"] = [{"fields": [title, platforms], "problems": []}]
    run = at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert shown["connector"] == "upload_post" and vals(shown)["platforms"] == [
        "tiktok",
        "instagram",
    ]
    _, tool, args, env = transport.calls[-1]
    assert (tool, args["params"]["user"], env) == ("preview", "me", {"UPLOAD_POST_API_KEY": "k"})

    transport.outputs["publish"] = [{"status": "scheduled", "id": "job_9", "links": {}}]
    running.post(f"/api/runs/{run['id']}/decision", json={})
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    _, tool, args, _ = transport.calls[-1]
    assert tool == "publish" and args["fields"]["platforms"] == ["tiktok", "instagram"]
    assert args["idempotency_key"] == run["stages"][2]["id"]
    assert args["input_path"].endswith(f"{run['id']}/01-render/short.mp4")
    stage_id = run["stages"][2]["id"]
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / stage_id / "artifacts"
    text = (folder / "publish.md").read_text()
    assert 'status: "scheduled"\n' in text and "upload_post job job_9 is scheduled" in text


@pytest.mark.parametrize("kind", ["confirm", "approve"])
def test_a_gate_before_a_broken_publish_stage_refuses_to_go_on(
    running: TestClient, transport: FakeTransport, tmp_path: Path, kind: str
) -> None:
    # the metadata file is missing only at run time; the check on save cannot see that
    yaml = weekly(extra="\n      metadata: script.md").replace("kind: confirm", f"kind: {kind}")
    world, workflow = setup(running, yaml)
    run = at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert any("script.md" in p for p in shown["problems"])
    refused = running.post(f"/api/runs/{run['id']}/decision", json={})
    assert refused.status_code == 409 and "script.md" in refused.json()["detail"]
    assert running.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate"
    assert [c[0] for c in transport.calls] == ["maker"]


SCRIPT = """---
title: "Why owls turn their heads"
description: |
  Owls cannot move their eyes.
  So they turn their heads.
hashtags: ["#Owls", "#AnimalFacts"]
tags: [owls, owl facts]
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

    preview = running.get("/api/gates").json()[0]["publish"]
    shown = vals(preview)
    assert shown["title"] == "Why owls turn their heads"
    assert shown["description"] == "Owls cannot move their eyes.\nSo they turn their heads."
    assert shown["made_for_kids"] is True and preview["problems"] == []
    assert shown["schedule"].endswith("T09:00:00Z")

    url = f"/api/runs/{run['id']}/decision"
    refused = running.post(url, json={"edits": {"title": "x" * 101}})
    assert refused.status_code == 422 and "the limit is 100" in refused.json()["detail"]
    fixed = running.post(url, json={"edits": {"visibility": "private"}})
    assert fixed.status_code == 422 and "visibility cannot be edited" in fixed.json()["detail"]
    transport.outputs["youtube"] = [{"video_id": "v", "url": "https://youtu.be/v"}]
    edited = {"title": "Owls, explained", "description": "Edited."}
    stale = {**shown, "schedule": "2099-01-01T09:00:00Z"}
    moved = running.post(url, json={"edits": edited, "seen": stale})
    assert moved.status_code == 422 and "When changed since" in moved.json()["detail"]
    assert running.post(url, json={"edits": edited, "seen": shown}).status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")

    _, _, args, _ = transport.calls[-1]
    assert (args["title"], args["description"]) == (
        "Owls, explained",
        "Edited.\n\n#Owls #AnimalFacts",
    )
    assert args["tags"] == ["owls", "owl facts"]
    assert (args["schedule"], args["made_for_kids"]) == (shown["schedule"], True)
    (decision,) = running.get(f"/api/runs/{run['id']}").json()["decisions"]
    assert decision["publish"] == {**shown, **edited}
    stage_id = run["stages"][2]["id"]
    artifact = tmp_path / "worlds" / world / "runs" / run["id"] / stage_id / "artifacts"
    assert (
        (artifact / "publish.md")
        .read_text()
        .split("\n---\n", 1)[1]
        .startswith("\n# Owls, explained")
    )


def test_an_emoji_in_the_metadata_title_reaches_the_gate_whole(
    running: TestClient, tmp_path: Path
) -> None:
    """Frontmatter is written with json.dumps, which escapes an emoji as a surrogate pair; the
    gate list once answered 500 on it, so the gate had no Approve button."""
    running.put("/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS})
    world, workflow = setup(running, weekly(extra="\n      metadata: script.md"))
    run = at_gate(running, tmp_path, world, workflow)
    folder = tmp_path / "worlds" / world / "runs" / run["id"] / "01-render"
    title = json.dumps("Why a cast saw can't cut you \N{FEARFUL FACE}")
    (folder / "script.md").write_text(f"---\ntitle: {title}\n---\n\n# Script\n")
    assert "\\ud83d\\ude28" in (folder / "script.md").read_text()

    gates = running.get("/api/gates")
    assert gates.status_code == 200
    title = vals(gates.json()[0]["publish"])["title"]
    assert title == "Why a cast saw can't cut you \N{FEARFUL FACE}"


def test_a_missing_metadata_artifact_is_a_problem_the_gate_shows(
    running: TestClient, tmp_path: Path
) -> None:
    world, workflow = setup(running, weekly(extra="\n      metadata: script.md"))
    at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert vals(shown)["title"] == "Five hooks that work"
    assert any("script.md" in p for p in shown["problems"])


@pytest.mark.parametrize("calls", ["youtube", "upload_post"])
def test_the_editor_refuses_a_publish_stage_without_a_gate(client: TestClient, calls: str) -> None:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    yaml = f"name: Sneaky\nstages:\n  - connector: publish\n    calls: {calls}\n"
    checked = client.post(f"/api/worlds/{world}/workflows/check", json={"yaml": yaml}).json()
    assert [v["rule"] for v in checked["violations"]][0] == "publish_without_gate"
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 422


def test_a_start_refuses_a_connector_that_turned_publish_after_the_save(
    running: TestClient, transport: FakeTransport
) -> None:
    """Today's workflow check at run start (#102) catches it before any stage runs."""
    yaml = "name: Sneaky\nstages:\n  - connector: post\n    calls: poster\n"
    poster = {**MAKER, "name": "poster"}
    world, workflow = setup(running, "name: x\nstages:\n  - connector: m\n    calls: maker\n")
    running.post(f"/api/worlds/{world}/connectors", json=poster).raise_for_status()
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml}).json()
    running.delete(f"/api/worlds/{world}/packages/poster")
    publishing = {**poster, "kind": "publish"}
    assert running.post(f"/api/worlds/{world}/connectors", json=publishing).status_code == 201

    refused = running.post(f"/api/workflows/{saved['workflow_id']}/runs", json={})
    assert refused.status_code == 409
    assert "publish connector 'poster' must come right after a gate" in refused.json()["detail"]
    assert running.get(f"/api/worlds/{world}/runs").json() == []
    assert transport.calls == []


def test_a_worlds_own_publish_connector_posts_text_through_the_same_gate(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    """The core holds the gate and knows no fields: a webhook posting a message works the same."""
    hook = {**MAKER, "name": "webhook", "kind": "publish", "server": "uvx webhook-mcp"}
    yaml = weekly("webhook").replace("input: short.mp4", "input: short.mp4\n    params: {}")
    yaml = yaml.split("    params:\n      title")[0]
    world = running.post("/api/worlds", json={"name": "News"}).json()["id"]
    for connector in (MAKER, hook):
        assert running.post(f"/api/worlds/{world}/connectors", json=connector).status_code == 201
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 201, saved.text
    workflow = saved.json()["workflow_id"]
    message = {"key": "text", "label": "Message", "value": "New video is up", "editable": True}
    transport.outputs["preview"] = [
        {"fields": [{"key": "to", "label": "To", "value": "#news"}, message], "problems": []}
    ]
    run = at_gate(running, tmp_path, world, workflow)
    shown = running.get("/api/gates").json()[0]["publish"]
    assert vals(shown) == {"to": "#news", "text": "New video is up"}

    transport.outputs["publish"] = [{"url": "https://hooks.example/m/1"}]
    edits = {"text": "It is up"}
    answer = running.post(f"/api/runs/{run['id']}/decision", json={"edits": edits})
    assert answer.status_code == 200
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    (decision,) = running.get(f"/api/runs/{run['id']}").json()["decisions"]
    assert decision["publish"] == {"to": "#news", "text": "It is up"}
    name, tool, args, _ = transport.calls[-1]
    assert (name, tool, args["fields"]) == ("webhook", "publish", decision["publish"])
    assert args["input_path"].endswith("01-render/short.mp4")
    # the connector hears the edits, and they stand though this fake ignores them
    assert edits in [c[2]["edits"] for c in transport.calls if c[1] == "preview"]
