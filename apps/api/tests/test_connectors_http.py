"""Connectors over HTTP: the registry, test-and-list-tools, and a connector stage's raw call,
with the fake ConnectorTransport and real DBOS on SQLite behind the app."""

import stat
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_api.main import create_app, migrated_db, world_service
from swarmify_connectors import ConnectorError, FakeTransport, Tool
from swarmify_kernel.sqlite import connect
from swarmify_workflows import ConnectorRef, Workflows
from swarmify_workflows import SqliteStorage as WorkflowStorage

FAL = {
    "name": "fal_video",
    "kind": "stage",
    "server": "uvx fal-mcp",
    "credentials": {"FAL_KEY": "s3cret"},
    "stage_types": ["connector"],
}
CLIP = """name: Clip then pick
stages:
  - connector: clip
    calls: fal_video
    output: clip.mp4
    params: {seconds: 5}
  - gate: pick
    ask: Keep it?
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def world(client: TestClient) -> str:
    return client.post("/api/worlds", json={"name": "Video"}).json()["id"]


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_web_search_and_the_graph_are_agent_tools_not_listed_connectors(
    client: TestClient,
) -> None:
    w = world(client)
    listed = client.get(f"/api/worlds/{w}/connectors").json()
    assert not {c["name"] for c in listed} & {"web_search", "graph"}
    saved = (
        "name: s\nstages:\n  - swarm: r\n    goal: g\n    done_when: d\n"
        "    tools: [web_search, graph]\n"
    )
    assert client.post(f"/api/worlds/{w}/workflows", json={"yaml": saved}).status_code == 201
    taken = client.post(f"/api/worlds/{w}/connectors", json={**FAL, "name": "graph"})
    assert taken.status_code == 422 and "Agent Tool" in taken.text
    assert client.get("/api/worlds/world_nope/connectors").status_code == 404


def test_test_lists_the_tools_of_a_draft_without_saving_it(
    client: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    w = world(client)
    transport.tools["fal_video"] = [Tool("fal_video", "Make a clip.", {"type": "object"})]
    tested = client.post(f"/api/worlds/{w}/connectors/test", json=FAL)
    assert tested.status_code == 200
    assert tested.json() == {
        "tools": [
            {"name": "fal_video", "description": "Make a clip.", "input_schema": {"type": "object"}}
        ]
    }
    assert transport.starts == [("fal_video", {"FAL_KEY": "s3cret"})]
    assert "fal_video" not in [c["name"] for c in client.get(f"/api/worlds/{w}/connectors").json()]
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"name": "Fal Video"}, "lower_snake_case"),
        ({"credentials": {"FAL_KEY": ""}}, "needs FAL_KEY"),
        ({"name": "nobody_home"}, "could not start"),
    ],
)
def test_test_explains_what_went_wrong(
    client: TestClient, over: dict[str, Any], problem: str
) -> None:
    answer = client.post(f"/api/worlds/{world(client)}/connectors/test", json={**FAL, **over})
    assert answer.status_code == 422 and problem in answer.json()["detail"]


def test_add_installs_a_local_package_whose_keys_are_credentials_never_returned(
    client: TestClient, tmp_path: Path
) -> None:
    w = world(client)
    added = client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert added.status_code == 201
    body = added.json()
    assert (body["name"], body["command"], body["credentials"], body["version"]) == (
        "fal_video",
        ["uvx", "fal-mcp"],
        ["FAL_KEY"],
        "0.0.1",
    )
    assert body["missing"] == [] and "s3cret" not in added.text
    assert [(p["name"], p["version"], p["source"]) for p in client.get("/api/packages").json()] == [
        ("fal_video", "0.0.1", "local"),
        ("remotion_render", "1.0.0", "connectors/remotion_render"),
        ("slack", "1.0.0", "connectors/slack"),
        ("speech", "1.0.0", "connectors/speech"),
        ("upload_post", "1.0.0", "connectors/upload_post"),
        ("youtube", "1.0.0", "connectors/youtube"),
    ]
    assert client.get(f"/api/worlds/{w}/packages").json() == [
        {"name": "fal_video", "version": "0.0.1"},
        {"name": "remotion_render", "version": "1.0.0"},
        {"name": "speech", "version": "1.0.0"},
        {"name": "upload_post", "version": "1.0.0"},
        {"name": "youtube", "version": "1.0.0"},
    ]
    form = client.get(f"/api/worlds/{w}/packages/fal_video/setup")
    (field,) = form.json()["fields"]
    assert (field["label"], field["env"], field["secret"], field["scope"]) == (
        "FAL_KEY",
        "FAL_KEY",
        True,
        "shared",
    )
    assert field["shared_set"] and "s3cret" not in form.text
    setup = tmp_path / "setup.json"
    assert "s3cret" in setup.read_text() and stat.S_IMODE(setup.stat().st_mode) == 0o600
    assert not (tmp_path / ".env").exists()
    listed = client.get(f"/api/worlds/{w}/connectors")
    assert "s3cret" not in listed.text and body["id"] in [c["id"] for c in listed.json()]

    again = client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert again.status_code == 422 and "enables already" in again.json()["detail"]
    assert client.post("/api/worlds/world_nope/connectors", json=FAL).status_code == 404


def test_every_connector_carries_a_label_capabilities_and_what_it_makes(
    client: TestClient,
) -> None:
    # ADR 0025: a skin picks a step's panel from these, never from the connector's name
    w = world(client)
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    shown = {
        c["name"]: (c["label"], c["capabilities"], [o["media_type"] for o in c["outputs"]])
        for c in client.get(f"/api/worlds/{w}/connectors").json()
    }
    assert shown == {
        "remotion_render": ("Remotion render", ["check"], ["video/mp4"]),
        "remotion_build": ("Remotion build", ["build_session"], ["video/mp4"]),
        "youtube": ("YouTube", ["publish", "outcomes"], []),
        "upload_post": ("Upload-Post", ["publish"], []),
        "speech": ("Speech", [], ["application/json", "audio/wav"]),
        "fal_video": ("fal_video", [], []),
    }


def test_workflows_see_the_registry(client: TestClient) -> None:
    w = world(client)
    swarm = (
        "name: s\nstages:\n  - swarm: r\n    goal: g\n    done_when: d\n    tools: [fal_video]\n"
    )
    assert client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).status_code == 422
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).status_code == 201
    refused = client.post(f"/api/worlds/{w}/workflows", json={"yaml": swarm})
    assert [v["rule"] for v in refused.json()["violations"]] == ["connector_stage_type"]


def test_an_added_connector_leaves_the_world_as_any_package_does(client: TestClient) -> None:
    w = world(client)
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    assert client.delete(f"/api/worlds/{w}/packages/fal_video").status_code == 204
    assert all(c["name"] != "fal_video" for c in client.get(f"/api/worlds/{w}/connectors").json())


def test_a_v1_data_directory_moves_its_world_added_connectors_to_local_packages(
    tmp_path: Path, transport: FakeTransport
) -> None:
    root = tmp_path / "v1"
    db = migrated_db(root)
    w = world_service(root).create("Video").id
    with closing(connect(db)) as conn:
        conn.execute(
            "INSERT INTO connectors (id, world_id, name, kind, command, url, credentials,"
            " stage_types, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("conn_1", w, "fal_video", "stage", '["uvx", "fal-mcp"]', None, '["FAL_KEY"]')
            + ('["connector"]', "2026-10-01T00:00:00+00:00"),
        )
    ref = ConnectorRef("fal_video", "stage", frozenset({"connector"}))
    saved = Workflows(WorkflowStorage(db), lambda _: {"fal_video": ref}).save(w, CLIP)
    env = root / ".env"
    env.write_text("FAL_KEY=s3cret\nYOUTUBE_CLIENT_ID=yt\n")
    env.chmod(0o600)

    app = create_app(root, transport=transport, poll_seconds=0.01, retro=False)
    with TestClient(app) as client:
        listed = client.get(f"/api/worlds/{w}/connectors").json()
        (fal,) = [c for c in listed if c["name"] == "fal_video"]
        assert (fal["name"], fal["version"], fal["missing"]) == ("fal_video", "0.0.1", [])
        assert env.read_text() == "YOUTUBE_CLIENT_ID=yt\n"
        with closing(connect(db)) as conn:
            assert conn.execute("SELECT count(*) FROM connectors").fetchone()[0] == 0

        run = client.post(f"/api/workflows/{saved.workflow_id}/runs", json={}).json()
        until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
        assert transport.calls[0][:2] == ("fal_video", "fal_video")
        assert transport.calls[0][3] == {"FAL_KEY": "s3cret"}


def start(client: TestClient) -> dict[str, Any]:
    w = world(client)
    client.post(f"/api/worlds/{w}/connectors", json=FAL)
    workflow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": CLIP}).json()
    return client.post(f"/api/workflows/{workflow['workflow_id']}/runs", json={}).json()


def test_a_connector_stage_records_its_raw_call_keyed_by_the_stage_id(
    running: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "fal_status"}]
    transport.outputs["fal_status"] = [
        {"status": "running", "progress": 0.5},
        {"status": "done", "url": "https://cdn/clip.mp4"},
    ]
    run = start(running)
    url = f"/api/runs/{run['id']}"
    until(lambda: running.get(url).json()["status"] == "waiting_gate")
    stage_id = running.get(url).json()["stages"][0]["id"]

    call = running.get(f"/api/stages/{stage_id}/call").json()
    files = f"worlds/{run['world_id']}/runs/{run['id']}/{stage_id}/artifacts"
    assert call["request"] == {
        "seconds": 5,
        "idempotency_key": stage_id,
        "output": "clip.mp4",
        "output_path": str(tmp_path / files / "clip.mp4"),
        "frontmatter": {"world": run["world_id"], "run": run["id"], "stage": stage_id},
        "assets_path": str(tmp_path / f"worlds/{run['world_id']}/assets"),
    }
    assert call["response"] == {"status": "done", "url": "https://cdn/clip.mp4"}
    assert (call["status"], call["polls"], call["tool"]) == ("done", 2, "fal_video")
    assert (call["run_id"], call["world_id"]) == (run["id"], run["world_id"])
    assert transport.calls[0][3] == {"FAL_KEY": "s3cret"}
    assert running.get(f"/api/stages/{run['stages'][1]['id']}/call").status_code == 404


def test_a_failing_connector_fails_the_run_with_its_error(
    running: TestClient, transport: FakeTransport
) -> None:
    transport.outputs["fal_video"] = [ConnectorError("fal_video: quota exceeded")]
    run = start(running)
    url = f"/api/runs/{run['id']}"
    until(lambda: running.get(url).json()["status"] == "failed")
    stage = running.get(url).json()["stages"][0]
    assert stage["error"] == "ConnectorError: fal_video: quota exceeded"
    call = running.get(f"/api/stages/{stage['id']}/call").json()
    assert (call["status"], call["error"]) == ("failed", "fal_video: quota exceeded")


def test_progress_streams_on_the_world_events(live_url: str, transport: FakeTransport) -> None:
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "fal_status"}]
    transport.outputs["fal_status"] = [
        {"status": "running", "progress": 0.5, "message": "frames"},
        {"status": "done"},
    ]
    w = httpx.post(f"{live_url}/api/worlds", json={"name": "Video"}).json()["id"]
    httpx.post(f"{live_url}/api/worlds/{w}/connectors", json=FAL).raise_for_status()
    workflow = httpx.post(f"{live_url}/api/worlds/{w}/workflows", json={"yaml": CLIP}).json()
    seen: list[tuple[str, float | None]] = []
    with httpx.stream("GET", f"{live_url}/api/worlds/{w}/events", timeout=10) as r:
        httpx.post(f"{live_url}/api/workflows/{workflow['workflow_id']}/runs", json={})
        for kind, _, data in sse_events(r):
            if kind == "connector_call.updated":
                seen.append((data["payload"]["status"], data["payload"]["progress"]))
                if seen[-1][0] == "done":
                    break
    assert seen == [("running", None), ("polling", 0.0), ("polling", 0.5), ("done", 1.0)]


def test_the_speech_package_shows_whether_each_engine_answers(client: TestClient) -> None:
    w = world(client)
    listed = {c["name"]: c for c in client.get(f"/api/worlds/{w}/connectors").json()}
    assert listed["speech"]["services"] == ["engines"] and listed["speech"]["version"] == "1.0.0"
    assert listed["youtube"]["services"] == [] and listed["youtube"]["health"] is None
    down = "http://127.0.0.1:9"  # nothing listens on discard
    setup = {"shared": {"speech_endpoints": down}, "world": {}}
    assert client.put(f"/api/worlds/{w}/packages/speech/setup", json=setup).status_code == 200
    (check,) = client.get(f"/api/worlds/{w}/packages/speech/health").json()
    assert check["name"] == down and check["ok"] is False and check["message"]
    assert client.get(f"/api/worlds/{w}/packages/echo/health").status_code == 404
    assert client.get("/api/worlds/nope/packages/speech/health").status_code == 404
    assert client.get("/api/connectors/builtin_remotion_build/health").json() == []
    assert client.get("/api/connectors/nope/health").status_code == 404


def test_install_a_package_from_this_repo_and_list_it(client: TestClient, tmp_path: Path) -> None:
    # the defaults: slack is installed too, but no world is given it
    assert [p["name"] for p in client.get("/api/packages").json()] == [
        "remotion_render",
        "slack",
        "speech",
        "upload_post",
        "youtube",
    ]
    source = "packages/connectors/tests/fixtures/packages/echo"
    installed = client.post("/api/packages", json={"source": source})
    assert installed.status_code == 201, installed.text
    assert (tmp_path / "packages/echo/1.0.0/installed.txt").is_file()
    listed = client.get("/api/packages").json()
    assert listed[0] == installed.json() and len(listed) == 6
    assert {k: listed[0][k] for k in ("name", "version", "label", "kind", "state", "source")} == {
        "name": "echo",
        "version": "1.0.0",
        "label": "Echo",
        "kind": "stage",
        "state": "installed",
        "source": source,
    }
    again = client.post("/api/packages", json={"source": source})
    assert again.status_code == 422
    assert again.json()["detail"] == "echo 1.0.0 is already installed"
    # installed for the whole install: no world holds it, and the built-ins are unchanged
    world_id = world(client)
    names = [c["name"] for c in client.get(f"/api/worlds/{world_id}/connectors").json()]
    assert "echo" not in names and "speech" in names


def test_an_invalid_manifest_is_refused_naming_the_field(
    client: TestClient, tmp_path: Path
) -> None:
    source = tmp_path / "bad"
    source.mkdir()
    fixture = Path(__file__).parents[3] / "packages/connectors/tests/fixtures/manifests"
    (source / "swarmify-connector.toml").write_text((fixture / "bad_kind.toml").read_text())
    refused = client.post("/api/packages", json={"source": str(source)})
    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("invalid Manifest: kind: 'robot'")
    assert [p["name"] for p in client.get("/api/packages").json()] == [
        "remotion_render",
        "slack",
        "speech",
        "upload_post",
        "youtube",
    ]


ECHO = """name: Say it
stages:
  - swarm: write
    goal: a line
    done_when: written
    output_type: line
    produces: [line.md]
  - connector: say
    calls: echo
    input: line.md
    output: said.wav
"""


def test_worlds_enable_a_package_at_a_pinned_version(client: TestClient) -> None:
    source = "packages/connectors/tests/fixtures/packages/echo"
    assert client.post("/api/packages", json={"source": source}).status_code == 201
    one, two = world(client), world(client)
    refused = client.post(f"/api/worlds/{one}/workflows", json={"yaml": ECHO})
    assert refused.status_code == 422
    assert refused.json()["violations"][0]["message"] == "no connector named 'echo'"

    for w in (one, two):
        put = client.put(f"/api/worlds/{w}/packages/echo", json={"version": "1.0.0"})
        assert put.status_code == 200 and put.json() == {"name": "echo", "version": "1.0.0"}
    assert client.get(f"/api/worlds/{one}/packages").json() == [
        {"name": "echo", "version": "1.0.0"},
        {"name": "remotion_render", "version": "1.0.0"},  # the default packages
        {"name": "speech", "version": "1.0.0"},
        {"name": "upload_post", "version": "1.0.0"},
        {"name": "youtube", "version": "1.0.0"},
    ]
    echo = next(
        c for c in client.get(f"/api/worlds/{one}/connectors").json() if c["name"] == "echo"
    )
    assert (echo["name"], echo["version"], echo["builtin"]) == ("echo", "1.0.0", False)
    assert "folder" not in echo
    assert echo["label"] == "Echo"  # from its Manifest
    assert client.post(f"/api/worlds/{one}/workflows", json={"yaml": ECHO}).status_code == 201

    assert client.delete(f"/api/worlds/{one}/packages/echo").status_code == 204
    assert client.get(f"/api/worlds/{one}/packages").json() == [
        {"name": "remotion_render", "version": "1.0.0"},
        {"name": "speech", "version": "1.0.0"},
        {"name": "upload_post", "version": "1.0.0"},
        {"name": "youtube", "version": "1.0.0"},
    ]
    assert client.get(f"/api/worlds/{two}/packages").json() == [
        {"name": "echo", "version": "1.0.0"},
        {"name": "remotion_render", "version": "1.0.0"},
        {"name": "speech", "version": "1.0.0"},
        {"name": "upload_post", "version": "1.0.0"},
        {"name": "youtube", "version": "1.0.0"},
    ]
    assert client.post(f"/api/worlds/{one}/workflows", json={"yaml": ECHO}).status_code == 422
    assert client.post(f"/api/worlds/{two}/workflows", json={"yaml": ECHO}).status_code == 201
    names = [c["name"] for c in client.get(f"/api/worlds/{one}/connectors").json()]
    assert "echo" not in names and "speech" in names and "youtube" in names

    assert client.delete(f"/api/worlds/{one}/packages/echo").status_code == 404
    missing = client.put(f"/api/worlds/{one}/packages/echo", json={"version": "9.0.0"})
    assert missing.status_code == 422 and missing.json()["detail"] == "echo 9.0.0 is not installed"
    nope = client.put("/api/worlds/world_nope/packages/echo", json={"version": "1.0.0"})
    assert nope.status_code == 404


def test_a_packages_setup_form_tests_before_saving_and_never_returns_a_credential(
    client: TestClient, transport: FakeTransport, tmp_path: Path
) -> None:
    source = "packages/connectors/tests/fixtures/packages/probe"
    assert client.post("/api/packages", json={"source": source}).status_code == 201
    one, two = world(client), world(client)
    for w in (one, two):
        client.put(f"/api/worlds/{w}/packages/probe", json={"version": "1.0.0"})
    form = client.get(f"/api/worlds/{one}/packages/probe/setup").json()
    assert [f["type"] for f in form["fields"]] == [
        "string", "text", "number", "integer", "boolean", "choice", "url", "path"
    ]  # fmt: skip
    key = form["fields"][0]
    assert (key["key"], key["secret"], key["required"], key["env"]) == (
        "api_key", True, True, "PROBE_KEY"
    )  # fmt: skip
    assert form["missing"] == ["api_key"] and form["test"] == "check_setup"
    probe = next(
        c for c in client.get(f"/api/worlds/{one}/connectors").json() if c["name"] == "probe"
    )
    assert probe["missing"] == ["api_key"] and probe["credentials"] == ["PROBE_KEY"]

    transport.outputs["check_setup"] = [
        {"ok": False, "message": "That key was refused."},
        {"ok": True, "message": "Signed in as probe."},
    ]
    url = f"/api/worlds/{one}/packages/probe/setup"
    body = {"shared": {"api_key": "s3cret", "ratio": "0.5"}, "world": {"voice": "high"}}
    refused = client.put(url, json=body)
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Not saved. The test said: That key was refused."
    assert not (tmp_path / "setup.json").exists()

    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["check"] == {"ok": True, "message": "Signed in as probe."}
    assert "s3cret" not in saved.text
    assert stat.S_IMODE((tmp_path / "setup.json").stat().st_mode) == 0o600
    for w, voice in ((one, "high"), (two, None)):
        got = client.get(f"/api/worlds/{w}/packages/probe/setup")
        assert "s3cret" not in got.text
        by_key = {f["key"]: f for f in got.json()["fields"]}
        assert (by_key["api_key"]["shared"], by_key["api_key"]["shared_set"]) == (None, True)
        assert by_key["ratio"]["shared"] == 0.5 and by_key["voice"]["world"] == voice
        assert got.json()["missing"] == []
    assert "s3cret" not in client.get(f"/api/worlds/{one}/connectors").text

    bad = client.put(url, json={"shared": {"ratio": "lots"}})
    assert bad.status_code == 422 and bad.json()["detail"] == "Ratio: is a number"
    gone = client.get(f"/api/worlds/{one}/packages/echo/setup")
    assert gone.status_code == 404
