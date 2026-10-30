"""Upgrading a V1 data directory (issue 213, ADR 0023): the speech connector became a default
Connector Package, installed and given to every world at startup, its engines moved from the app's
env into its shared Setup, and a workflow saved in V1 with a speech stage runs unchanged."""

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

import pytest
from fastapi.testclient import TestClient
from test_typed_http import _wav

from swarmify_api import main
from swarmify_kernel import Event, new_id
from swarmify_workflows import Workflow
from swarmify_workflows.sqlite import SqliteStorage as WorkflowStorage

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
    called the built-in, no packages folder, no Setup, and no package versions recorded."""
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
            {"name": "script_writer", "version": "0.0.1"},  # the world's own, moved (#211)
            {"name": "speech", "version": "1.0.0"},
        ]
        version = client.get(f"/api/workflows/{flow}/versions/1").json()
        assert version["packages"] == {"script_writer": "0.0.1", "speech": "1.0.0"}  # stamped
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
        "script_writer"
    ]
    new = again.post("/api/worlds", json={"name": "New"}).json()["id"]
    assert again.get(f"/api/worlds/{new}/packages").json() == [
        {"name": "speech", "version": "1.0.0"}
    ]
