"""The Build Session Capability is generic (ADR 0018, issue 217): the fixture package `sketch`
requests one in its Manifest, and a run through the API gives it a sandboxed session with its
system prompt, brief, starter and network, charged to the run's budget, then calls its own
`finish` tool on the result as a long job. Real DBOS on SQLite, the fake ConnectorTransport and
the Agent SDK client with a scripted CLI."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from api_fakes import install
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import Connector, FakeTransport
from swarmify_swarm.fake import Answer, Say, Script

SKETCH = "packages/connectors/tests/fixtures/packages/sketch"
FLOW = """name: A page
stages:
  - connector: copy
    calls: draft
    output: copy.md
  - connector: colours
    calls: draft
    output: palette.json
  - connector: page
    calls: sketch
    inputs: [copy.md, palette.json]
    output: page.html
    params: {tone: loud, brief: Big letters.}
"""
DRAFT = {"name": "draft", "kind": "stage", "server": "uvx draft-mcp", "stage_types": ["connector"]}


class Writing(FakeTransport):
    """Writes each stage's output, so the stage after it finds its input."""

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        if arguments.get("output_path") and tool != "finish":
            Path(arguments["output_path"]).write_text("written")
        return super().call(connector, tool, arguments, env)


@pytest.fixture
def transport() -> FakeTransport:
    def done(args: dict[str, Any]) -> dict[str, Any]:
        Path(args["job_id"]).write_text("<html>published</html>")
        return {"status": "done"}

    return Writing(
        outputs={
            "finish": [lambda a: {"job_id": a["output_path"], "poll": "finish_status"}],
            "finish_status": [{"status": "running", "progress": 0.5}, done],
        }
    )


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_package_requesting_a_build_session_gets_one_then_its_tool_runs_on_the_result(
    running: TestClient, transport: FakeTransport, script: Script, tmp_path: Path
) -> None:
    client = running
    script.scripts = [[Answer(lambda o: (Path(o.cwd) / "page.css").write_text("x")), Say("Done.")]]
    script.cost = 0.25
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    enabled = client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"})
    assert enabled.status_code == 200, enabled.text
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": FLOW})
    assert saved.status_code == 201, saved.text
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "done", run

    copy_id, palette_id, page_id = (s["id"] for s in run["stages"])
    folder = tmp_path / "worlds" / w / "runs" / run["id"]
    build = folder / page_id / "build"
    copy, palette = (
        folder / copy_id / "artifacts/copy.md",
        folder / palette_id / "artifacts/palette.json",
    )

    # the session: the package's system prompt, brief and note, seeded from its starter, its
    # network only, sandboxed, and paid from the run's budget
    (options,) = script.sessions
    assert options.cwd == build
    assert options.system_prompt == "You are the page writer of the sketch fixture.\n"
    assert (options.sandbox or {}).get("network") == {"allowedDomains": ["pypi.org"]}
    assert (options.sandbox or {}).get("allowUnsandboxedCommands") is False
    prompt = script.prompts[0]
    assert prompt.startswith(f"Write a loud page for the copy at {copy}.")
    assert f"Use the colours in {palette} (palette.json)." in prompt
    assert f"The stage's other inputs, also yours to read: {palette}." in prompt
    assert "The operator's brief: Big letters." in prompt
    assert (build / "index.html").is_file() and (build / "page.css").is_file()
    assert run["spent_usd"] == pytest.approx(0.25)

    # then its own tool on the result, a long job polled to the end, its output the artifact
    finish = [c for c in transport.calls if c[1] == "finish"]
    assert [(c[0], c[2]["site"], c[2]["tone"]) for c in finish] == [("sketch", str(build), "loud")]
    assert finish[0][2]["input_path"] == str(copy)
    assert [c[1] for c in transport.calls if c[0] == "sketch"] == ["finish"] + ["finish_status"] * 2
    call = client.get(f"/api/stages/{page_id}/call").json()
    assert (call["connector"], call["tool"], call["status"]) == ("sketch", "finish", "done")
    [artifact] = client.get(f"/api/runs/{run['id']}/stages/{page_id}/artifacts").json()
    assert artifact["name"] == "page.html"
