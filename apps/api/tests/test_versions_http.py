"""Versions and upgrades over HTTP (issue 223), with the fixture package's two versions: a
workflow saved on 1.0.0 keeps running it after its world moves to 2.0.0, the switch shows the
Manifest diff and waits for a new required Setup field, an uninstall of a version in use is
refused naming its users, and versions saved before versions were recorded are stamped."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_api import main
from swarmify_connectors import Connector, FakeTransport

V1 = "packages/connectors/tests/fixtures/packages/echo"
V2 = "packages/connectors/tests/fixtures/packages/echo_2"
SAY = """name: Say it
stages:
  - connector: draft
    output: line.md
  - connector: say
    calls: echo
    input: line.md
    output: said.wav
    params: {tool: echo}
"""
DRAFT = {"name": "draft", "kind": "stage", "server": "uvx draft-mcp", "stage_types": ["connector"]}
"""A world's own connector writing the line echo says, as its Manifest asks for an input."""


class Writing(FakeTransport):
    """Writes each stage's output, so the stage after it finds its input."""

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        if arguments.get("output_path"):
            Path(arguments["output_path"]).write_text("a line")
        return super().call(connector, tool, arguments, env)


@pytest.fixture
def transport() -> FakeTransport:
    return Writing()


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def world(client: TestClient, name: str) -> str:
    w = client.post("/api/worlds", json={"name": name}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    return w


def pin(client: TestClient, w: str, version: str) -> Any:
    return client.put(f"/api/worlds/{w}/packages/echo", json={"version": version})


def run_once(client: TestClient, workflow_id: str, number: int) -> None:
    run = client.post(f"/api/workflows/{workflow_id}/runs", json={"version": number}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "done")


def test_a_workflow_saved_on_v1_keeps_running_v1_after_the_world_moves_to_v2(
    running: TestClient, transport: FakeTransport
) -> None:
    client = running
    for source in (V1, V2):
        assert client.post("/api/packages", json={"source": source}).status_code == 201
    w = world(client, "Studio")
    assert pin(client, w, "1.0.0").status_code == 200
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": SAY}).json()
    assert saved["packages"]["echo"] == "1.0.0"
    assert pin(client, w, "2.0.0").status_code == 422  # it waits for 2.0.0's new Region
    values = {"shared": {"api_key": "k"}, "world": {"region": "eu"}}
    setup = f"/api/worlds/{w}/packages/echo/setup"
    filled = client.put(setup, params={"version": "2.0.0"}, json=values)
    assert filled.status_code == 200 and filled.json()["version"] == "2.0.0"
    assert client.get(setup).json()["version"] == "1.0.0"  # the pin has not moved
    assert pin(client, w, "2.0.0").status_code == 200
    url = f"/api/workflows/{saved['workflow_id']}/versions"
    again = client.post(url, json={"yaml": SAY}).json()
    assert again["packages"]["echo"] == "2.0.0"

    transport.calls.clear()  # what the Setup test tool saw
    transport.versions.clear()
    run_once(client, saved["workflow_id"], 1)
    run_once(client, saved["workflow_id"], 2)
    ran = [v for c, v in zip(transport.calls, transport.versions, strict=True) if c[1] == "echo"]
    assert ran == ["1.0.0", "2.0.0"]  # each run's echo call


def test_the_switch_shows_the_diff_and_waits_for_a_new_required_field(client: TestClient) -> None:
    for source in (V1, V2):
        assert client.post("/api/packages", json={"source": source}).status_code == 201
    w = world(client, "Studio")
    assert pin(client, w, "1.0.0").status_code == 200
    diff = client.get(f"/api/worlds/{w}/packages/echo/switch", params={"version": "2.0.0"})
    assert diff.json() == {
        "from": "1.0.0",
        "to": "2.0.0",
        "params": {"added": ["speed"], "removed": [], "changed": []},
        "setup": {"added": ["region"], "removed": [], "changed": []},
        "required": ["region"],
        "permissions": {"added": ["network: eu.example.org"], "removed": [], "changed": []},
        "needs": ["Region"],
    }
    refused = pin(client, w, "2.0.0")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "echo 2.0.0 needs a value for Region first"
    assert {"name": "echo", "version": "1.0.0"} in client.get(f"/api/worlds/{w}/packages").json()
    gone = client.get(f"/api/worlds/{w}/packages/echo/switch", params={"version": "9.0.0"})
    assert gone.status_code == 422


def test_uninstalling_a_version_in_use_is_refused_naming_its_users(client: TestClient) -> None:
    for source in (V1, V2):
        assert client.post("/api/packages", json={"source": source}).status_code == 201
    studio, other = world(client, "Studio"), world(client, "Other")
    pin(client, studio, "1.0.0")
    saved = client.post(f"/api/worlds/{studio}/workflows", json={"yaml": SAY}).json()
    pin(client, other, "1.0.0")

    refused = client.delete("/api/packages/echo/1.0.0")
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "echo 1.0.0 is in use: world Studio pins it; world Other pins it;"
        " Say it v1 in world Studio was saved with it"
    )
    client.delete(f"/api/worlds/{studio}/packages/echo")
    client.delete(f"/api/worlds/{other}/packages/echo")
    still = client.delete("/api/packages/echo/1.0.0")
    assert still.status_code == 409 and still.json()["detail"].endswith("was saved with it")

    assert client.delete(f"/api/worlds/{studio}").status_code == 204  # its workflows go too
    assert client.delete("/api/packages/echo/1.0.0").status_code == 204
    echoes = [p["version"] for p in client.get("/api/packages").json() if p["name"] == "echo"]
    assert echoes == ["2.0.0"]
    assert client.delete("/api/packages/echo/1.0.0").status_code == 404
    assert saved["packages"]["echo"] == "1.0.0"


def test_versions_saved_before_versions_were_recorded_are_stamped_on_upgrade(
    tmp_path: Path, client: TestClient
) -> None:
    assert client.post("/api/packages", json={"source": V1}).status_code == 201
    w = world(client, "Studio")
    pin(client, w, "1.0.0")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": SAY}).json()
    with sqlite3.connect(tmp_path / "swarmify.db") as conn:  # as saved before this ticket
        conn.execute("UPDATE workflow_versions SET packages = '{}'")

    upgraded = TestClient(main.create_app(tmp_path, transport=FakeTransport(), retro=False))
    url = f"/api/workflows/{saved['workflow_id']}/versions/1"
    assert upgraded.get(url).json()["packages"]["echo"] == "1.0.0"
