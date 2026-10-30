"""A script stage then the Remotion render stage, end to end through the API: real DBOS on
SQLite, the real MCP transport and render wrapper, and a fake Remotion CLI on PATH."""

import json
import os
import shlex
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_api.main import create_app, with_files
from swarmify_connectors import ConnectorError
from swarmify_runs import Run, RunStageView

HERE = Path(__file__).parent
FAKE_NPX = HERE.parents[2] / "connectors/remotion_render/fake_npx.py"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "data", poll_seconds=0.05, retro=False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A template in the templates folder (the data directory's remotion/), its dependencies
    installed (a fake Remotion CLI, fake_npx.py), and a Chrome Headless Shell on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shell = bin_dir / "chrome-headless-shell"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    template = tmp_path / "data" / "remotion" / "video"
    cli = template / "node_modules" / ".bin" / "remotion"
    cli.parent.mkdir(parents=True)
    cli.write_text(f"#!{sys.executable}\n" + FAKE_NPX.read_text())
    cli.chmod(0o755)
    return template


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def render_run(client: TestClient, project: Path, composition: str, review: str = "") -> dict:
    w = client.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    server = shlex.join([sys.executable, str(HERE / "script_server.py")])
    added = client.post(
        f"/api/worlds/{w}/connectors",
        json={
            "name": "script_writer",
            "kind": "stage",
            "server": server,
            "stage_types": ["connector"],
        },
    )
    assert added.status_code == 201, added.text
    yaml = f"""name: Script then render
stages:
  - connector: script
    calls: script_writer
    output: script.md
  - connector: render
    calls: remotion_render
    input: script.md
    output: short.mp4
    params: {{project: "{project}", composition: {composition}}}
{review}"""
    workflow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
    assert workflow.status_code == 201, workflow.text
    run = client.post(f"/api/workflows/{workflow.json()['workflow_id']}/runs", json={}).json()
    return settled(client, run["id"])


def settled(client: TestClient, run_id: str) -> dict:
    """The run once it ends or waits at a gate."""
    ends = ("done", "failed", "waiting_gate")
    deadline = time.monotonic() + 30
    while (run := client.get(f"/api/runs/{run_id}").json())["status"] not in ends:
        assert time.monotonic() < deadline, run
        time.sleep(0.05)
    return run


def test_a_script_artifact_renders_into_a_video_with_a_sidecar(
    running: TestClient, project: Path, tmp_path: Path
) -> None:
    run = render_run(running, project, "Short")
    assert run["status"] == "done", run
    script_id, render_id = run["stages"][0]["id"], run["stages"][1]["id"]
    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    video = folder / render_id / "artifacts/short.mp4"
    rendered = json.loads(video.read_text())
    assert rendered["script"].startswith("# Why the chase works")
    assert rendered["scriptPath"] == str(folder / script_id / "artifacts/script.md")
    sidecar = (folder / render_id / "artifacts/short.mp4.md").read_text()
    assert f'run: "{run["id"]}"' in sidecar and f'stage: "{render_id}"' in sidecar

    call = running.get(f"/api/stages/{render_id}/call").json()
    assert call["status"] == "done" and call["progress"] == 1.0
    assert call["response"]["output"] == str(video)

    url = f"/api/runs/{run['id']}/stages/{render_id}/artifacts"
    [artifact] = running.get(url).json()
    assert (artifact["name"], artifact["type"], artifact["author"]) == (
        "short.mp4",
        "video",
        "remotion_render",
    )


def test_a_send_back_past_the_render_renders_the_new_script(
    running: TestClient, project: Path, tmp_path: Path
) -> None:
    review = "  - gate: review\n    ask: Watch it.\n    kind: approve\n"
    run = render_run(running, project, "Short", review)
    assert run["status"] == "waiting_gate", run
    script_id, render_id = run["stages"][0]["id"], run["stages"][1]["id"]
    sent = running.post(
        f"/api/runs/{run['id']}/decision", json={"note": "Again.", "send_back_to": "script"}
    )
    assert sent.status_code == 200, sent.text
    while (run := settled(running, run["id"]))["send_backs"] != 1:
        time.sleep(0.05)
    assert run["status"] == "waiting_gate", run

    folder = tmp_path / "data/worlds" / run["world_id"] / "runs" / run["id"]
    script = (folder / script_id / "artifacts/script.md").read_text()
    assert script.endswith(f"<!-- {script_id}.1 -->\n")
    rendered = json.loads((folder / render_id / "artifacts/short.mp4").read_text())
    assert rendered["script"] == script
    call = running.get(f"/api/stages/{render_id}/call").json()
    assert call["visit"] == 1 and call["request"]["idempotency_key"] == f"{render_id}.1"


def test_a_failed_render_puts_the_cli_output_on_the_timeline(
    running: TestClient, project: Path
) -> None:
    run = render_run(running, project, "Broken")
    assert run["status"] == "failed"
    render = run["stages"][1]
    assert render["status"] == "failed"
    assert render["error"].startswith("ConnectorError: remotion_render: remotion render exited")
    assert "No composition with the ID Broken found." in render["error"]
    call = running.get(f"/api/stages/{render['id']}/call").json()
    assert call["status"] == "failed" and "Bundling" in call["error"]


def test_a_render_without_an_input_is_refused(running: TestClient, project: Path) -> None:
    w = running.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    yaml = """name: Render only
stages:
  - connector: render
    calls: remotion_render
    output: short.mp4
    params: {project: /tmp, composition: Short}
"""
    refused = running.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
    assert refused.status_code == 422
    assert [v["message"] for v in refused.json()["violations"]] == [
        "remotion_render needs an input, the script"
    ]


def _run(*names: str) -> Run:
    stages = [RunStageView(f"stage_{i}", i, n, "connector", "pending") for i, n in enumerate(names)]
    return Run("run_1", "world_1", "wf_1", 1, "r", "running", "t", "t", stages)


def test_input_is_the_newest_run_file_of_that_name(tmp_path: Path) -> None:
    run = _run("draft", "rewrite", "render")
    for n, stage in enumerate(("stage_0", "stage_1")):
        (tmp_path / "runs/run_1" / stage / "artifacts").mkdir(parents=True)
        script = tmp_path / "runs/run_1" / stage / "artifacts/script.md"
        script.write_text(stage)
        os.utime(script, (n, n))
    config = with_files(tmp_path, run, "stage_2", {"input": "script.md", "output": "a.mp4"})
    assert Path(config["params"]["input_path"]).read_text() == "stage_1"
    assert config["params"]["output_path"] == str(tmp_path / "runs/run_1/stage_2/artifacts/a.mp4")
    assert Path(config["params"]["output_path"]).parent.is_dir()
    assert config["params"]["frontmatter"] == {
        "world": "world_1",
        "run": "run_1",
        "stage": "stage_2",
    }
    assert config["params"]["assets_path"] == str(tmp_path / "assets")
    assert (tmp_path / "assets").is_dir()  # a world older than its assets folder gets one


def test_an_input_no_earlier_stage_left_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError, match="no earlier stage of this run left a file named"):
        with_files(tmp_path, _run("draft", "render"), "stage_1", {"input": "script.md"})


@pytest.mark.parametrize("key", ["input", "output"])
@pytest.mark.parametrize("name", ["../../../../../escaped.mp4", "a/b.mp4", "..", "/etc/x"])
def test_a_file_name_that_is_a_path_is_refused(tmp_path: Path, key: str, name: str) -> None:
    with pytest.raises(ConnectorError, match="must be a bare file name"):
        with_files(tmp_path, _run("draft", "render"), "stage_1", {key: name})
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("key", ["input_path", "output_path", "frontmatter", "assets_path"])
def test_params_may_not_set_the_run_paths(tmp_path: Path, key: str) -> None:
    config = {"output": "clip.mp4", "params": {key: "/etc/passwd"}}
    with pytest.raises(ConnectorError, match=f"params.{key} is set by the run"):
        with_files(tmp_path, _run("draft", "render"), "stage_1", config)


def test_a_stage_with_several_inputs_gets_the_newest_of_each_across_a_send_back(
    running: TestClient, tmp_path: Path
) -> None:
    w = running.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    server = shlex.join([sys.executable, str(HERE / "script_server.py")])
    body = {"name": "script_writer", "kind": "stage", "server": server}
    added = running.post(f"/api/worlds/{w}/connectors", json={**body, "stage_types": ["connector"]})
    assert added.status_code == 201, added.text
    yaml = """name: Two inputs
stages:
  - connector: script
    calls: script_writer
    output: script.md
  - connector: notes
    calls: script_writer
    output: notes.md
  - connector: join
    calls: script_writer
    inputs: [script.md, notes.md]
    output: both.md
  - gate: review
    ask: Read it.
"""
    workflow = running.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
    assert workflow.status_code == 201, workflow.text
    run = running.post(f"/api/workflows/{workflow.json()['workflow_id']}/runs", json={}).json()
    run = settled(running, run["id"])
    assert run["status"] == "waiting_gate", run
    script_id, notes_id, join_id = (s["id"] for s in run["stages"][:3])
    folder = tmp_path / "data/worlds" / w / "runs" / run["id"]
    call = running.get(f"/api/stages/{join_id}/call").json()
    assert call["request"]["inputs"] == ["script.md", "notes.md"]
    assert call["request"]["input_paths"] == [
        str(folder / script_id / "artifacts/script.md"),
        str(folder / notes_id / "artifacts/notes.md"),
    ]
    both = (folder / join_id / "artifacts/both.md").read_text()
    assert f"<!-- {script_id} -->" in both and f"<!-- {notes_id} -->" in both

    sent = running.post(
        f"/api/runs/{run['id']}/decision", json={"note": "Again.", "send_back_to": "script"}
    )
    assert sent.status_code == 200, sent.text
    while (run := settled(running, run["id"]))["send_backs"] != 1:
        time.sleep(0.05)
    both = (folder / join_id / "artifacts/both.md").read_text()
    assert f"<!-- {script_id}.1 -->" in both and f"<!-- {notes_id}.1 -->" in both


def test_inputs_are_the_newest_run_file_of_each_name_in_order(tmp_path: Path) -> None:
    run = _run("draft", "voice", "rewrite", "video")
    for n, (stage, name) in enumerate(
        [("stage_0", "script.md"), ("stage_1", "timings.json"), ("stage_2", "script.md")]
    ):
        (tmp_path / "runs/run_1" / stage / "artifacts").mkdir(parents=True)
        file = tmp_path / "runs/run_1" / stage / "artifacts" / name
        file.write_text(stage)
        os.utime(file, (n, n))
    config = {"inputs": ["timings.json", "script.md"]}
    params = with_files(tmp_path, run, "stage_3", config)["params"]
    assert [Path(p).read_text() for p in params["input_paths"]] == ["stage_1", "stage_2"]
    assert "input_path" not in params
    with pytest.raises(ConnectorError, match="no earlier stage of this run left a file named"):
        with_files(tmp_path, run, "stage_3", {"inputs": ["script.md", "takes.wav"]})
    with pytest.raises(ConnectorError, match="must be a bare file name"):
        with_files(tmp_path, run, "stage_3", {"inputs": ["script.md", "../x.md"]})
    with pytest.raises(ConnectorError, match="params.input_paths is set by the run"):
        with_files(tmp_path, run, "stage_3", {"params": {"input_paths": ["/etc/passwd"]}})
