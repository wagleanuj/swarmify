"""Upgrading a V1 data directory (issues 213, 214 and 215, ADR 0023): the speech, Upload-Post and
youtube connectors became default Connector Packages, installed and given to every world at
startup. Speech's engines moved from the app's env and Upload-Post's key from the credentials file
into their shared Setup, and a workflow saved in V1 with a speech stage runs unchanged. YouTube's
app and the refresh token V1's terminal `authorize` printed moved from `.env` into its Google
sign-in, so it needs no new sign-in."""

import base64
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_typed_http import _wav

from swarmify_api import main
from swarmify_connectors import oauth
from swarmify_connectors.fake_oauth import FakeProvider
from swarmify_kernel import Event, new_id
from swarmify_workflows import Workflow
from swarmify_workflows.sqlite import SqliteStorage as WorkflowStorage

# These tests start a default package's real `.venv` process, so its install step runs.
real_default_installs = True

V1_WORKFLOW = """name: Bedtime
stages:
  - connector: write
    calls: script_writer
    output: script.md
  - connector: voice
    calls: speech
    input: script.md
    output: timings.json
"""


class Engine(BaseHTTPRequestHandler):
    """A speech engine answering Kokoro-FastAPI's captioned speech: 0.3 s a word."""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"status": "ok"})

    def do_POST(self) -> None:
        words = json.loads(self.rfile.read(int(self.headers["content-length"])))["input"].split()
        stamps = [
            {"word": w, "start_time": i * 0.3, "end_time": i * 0.3 + 0.25}
            for i, w in enumerate(words)
        ]
        audio = base64.b64encode(_wav(0.3 * len(words) + 0.3)).decode()
        self._send({"audio": audio, "timestamps": stamps})


@pytest.fixture
def engine() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def v1_data_dir(root: Path) -> tuple[str, str]:
    """What V1 left: a world with its own script writer and a saved workflow whose speech stage
    called the built-in, Upload-Post's key in the credentials file, no packages folder, no Setup,
    and no package versions recorded."""
    world = main.world_service(root).create("Bedtime")  # no defaults given: V1 had none
    command = [sys.executable, str(Path(__file__).parent / "script_server.py")]
    with sqlite3.connect(main.migrated_db(root)) as conn:  # V1's row for a world's connector
        conn.execute(
            "INSERT INTO connectors (id, world_id, name, kind, command, url, credentials,"
            " stage_types, created_at, types, input_types, world_types)"
            " VALUES (?, ?, 'script_writer', 'stage', ?, NULL, '[]', '[\"connector\"]', ?,"
            " '{}', '[]', NULL)",
            (new_id("conn"), world.id, json.dumps(command), "2026-10-28T09:00:00+00:00"),
        )
    flow = Workflow(new_id("wf"), world.id, "Bedtime", 1, "2026-10-28T09:00:00+00:00")
    saved = Event.new("workflow.saved", world_id=world.id, payload={"workflow_id": flow.id})
    WorkflowStorage(main.migrated_db(root)).add_workflow(flow, V1_WORKFLOW, saved)
    (root / ".env").write_text("UPLOAD_POST_API_KEY=up-k3y\n")
    (root / ".env").chmod(0o600)
    assert not (root / "packages").exists() and not (root / "setup.json").exists()
    return world.id, flow.id


def test_a_v1_workflow_with_a_speech_stage_runs_unchanged_after_upgrade(
    tmp_path: Path, engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, flow = v1_data_dir(tmp_path)
    monkeypatch.setenv("SPEECH_ENDPOINTS", engine)  # as V1's app env set it

    app = main.create_app(tmp_path, poll_seconds=0.05, retro=False)  # the upgrade
    with TestClient(app) as client:
        assert client.get(f"/api/worlds/{world}/packages").json() == [
            {"name": "remotion_build", "version": "1.0.0"},
            {"name": "remotion_render", "version": "1.0.0"},
            {"name": "script_writer", "version": "0.0.1"},  # the world's own, moved (#211)
            {"name": "speech", "version": "1.0.0"},
            {"name": "upload_post", "version": "1.0.0"},
            {"name": "youtube", "version": "1.1.0"},
        ]
        version = client.get(f"/api/workflows/{flow}/versions/1").json()
        assert version["packages"] == {"script_writer": "0.0.1", "speech": "1.0.0"}  # stamped
        (key,) = client.get(f"/api/worlds/{world}/packages/upload_post/setup").json()["fields"]
        assert (key["env"], key["shared_set"], key["shared"]) == ("UPLOAD_POST_API_KEY", True, None)
        posts = next(
            c
            for c in client.get(f"/api/worlds/{world}/connectors").json()
            if c["name"] == "upload_post"
        )
        assert posts["missing"] == [] and "up-k3y" not in json.dumps(posts)
        setup = client.get(f"/api/worlds/{world}/packages/speech/setup").json()
        (engines,) = setup["fields"]
        assert (engines["env"], engines["shared"]) == ("SPEECH_ENDPOINTS", engine)
        (check,) = client.get(f"/api/worlds/{world}/packages/speech/health").json()
        assert check == {"name": engine, "ok": True, "message": "up"}

        run = client.post(f"/api/workflows/{flow}/runs", json={"version": 1}).json()
        deadline = time.monotonic() + 60
        while (got := client.get(f"/api/runs/{run['id']}").json())["status"] != "done":
            assert got["status"] not in ("failed", "cancelled"), got
            assert time.monotonic() < deadline, got
            time.sleep(0.1)

    voiced = next((tmp_path / "worlds" / world / "runs" / run["id"]).rglob("timings.json"))
    timings = json.loads(voiced.read_text())
    assert timings["engine"] == engine and timings["takes"]
    assert all((voiced.parent / t["file"]).is_file() for t in timings["takes"])

    # Restarted, nothing is given twice: a world that disabled speech keeps it disabled.
    off = TestClient(main.create_app(tmp_path, retro=False))
    assert off.delete(f"/api/worlds/{world}/packages/speech").status_code == 204
    again = TestClient(main.create_app(tmp_path, retro=False))
    assert [p["name"] for p in again.get(f"/api/worlds/{world}/packages").json()] == [
        "remotion_build",
        "remotion_render",
        "script_writer",
        "upload_post",
        "youtube",
    ]
    new = again.post("/api/worlds", json={"name": "New"}).json()["id"]
    assert again.get(f"/api/worlds/{new}/packages").json() == [
        {"name": "remotion_build", "version": "1.0.0"},
        {"name": "remotion_render", "version": "1.0.0"},
        {"name": "speech", "version": "1.0.0"},
        {"name": "upload_post", "version": "1.0.0"},
        {"name": "youtube", "version": "1.1.0"},
    ]


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    """Google's token endpoint, played by the fake provider, holding the refresh token V1 kept."""
    post = httpx.post

    def to_fake(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://oauth2.googleapis.com/token"
        return post(f"{provider.url}/token", **kwargs)

    with FakeProvider(client_id="yt-app", client_secret="yt-secret") as provider:
        provider.refresh_tokens.add("rt-from-v1")
        monkeypatch.setattr(oauth.httpx, "post", to_fake)
        yield provider


def test_a_v1_youtube_sign_in_moves_into_the_package_with_no_new_sign_in(
    tmp_path: Path, google: FakeProvider
) -> None:
    world, _ = v1_data_dir(tmp_path)
    env = tmp_path / ".env"  # V1's keys, the refresh token as `authorize` printed it
    env.write_text(
        "YOUTUBE_CLIENT_ID=yt-app\nYOUTUBE_CLIENT_SECRET=yt-secret\n"
        "YOUTUBE_REFRESH_TOKEN=rt-from-v1\n"
    )
    env.chmod(0o600)

    with TestClient(main.create_app(tmp_path, retro=False)) as client:  # the upgrade
        form = client.get(f"/api/worlds/{world}/packages/youtube/setup")
        fields = {f["key"]: f for f in form.json()["fields"]}
        assert (
            [k for k, f in fields.items() if f["shared_set"]]
            == list(fields)
            == [
                "youtube_client_id",
                "youtube_client_secret",
                "youtube",
            ]
        )
        assert not fields["youtube"]["needs_sign_in"] and "rt-from-v1" not in form.text
        (row,) = [c for c in client.get(f"/api/worlds/{world}/connectors").json()
                  if c["name"] == "youtube"]  # fmt: skip
        assert (row["missing"], row["sign_in"]) == ([], [])

        # Its first start trades V1's refresh token for an access token; its own preview,
        # over stdio MCP, checks a publish stage as V1's built-in did.
        yaml = (
            "name: Weekly\nstages:\n  - gate: ok\n    ask: Post it?\n    kind: confirm\n"
            "  - connector: publish\n    calls: youtube\n    input: short.mp4\n"
            "    params: {visibility: secret}\n"
        )
        checked = client.post(f"/api/worlds/{world}/workflows/check", json={"yaml": yaml})
        said = [v["message"] for v in checked.json()["violations"] if v["stage"] == "publish"]
        assert {
            "a publish stage needs params.title",
            "visibility is one of public, unlisted, private, got 'secret'",
        } <= set(said)
    assert google.grants == ["refresh_token"]
    saved = json.loads((tmp_path / "setup.json").read_text())["youtube"]["shared"]["youtube"]
    assert saved["access_token"] in google.access_tokens  # kept, with the refresh token it gave
    assert saved["refresh_token"] in google.refresh_tokens
