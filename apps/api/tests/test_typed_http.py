"""Typed contracts (issue 133) through the API: a connector declares the type of its input, the
workflow check and the run start refuse a swarm stage that produces it without promising that
`output_type`, and a promised one is projected by the judge in the type's schema. A fixture
connector declaring a recipe shows the core knows nothing of video; the speech connector then
voices a projected script end to end, and remotion_build builds from it."""

import base64
import io
import json
import shutil
import threading
import wave
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_build_http import _installs, fake_cli, opened, settled  # noqa: F401  (fixtures)

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_connectors import ConnectorError, FakeTransport
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

# These tests start a default package's real `.venv` process, so its install step runs.
real_default_installs = True

RECIPE = {
    "type": "object",
    "properties": {"steps": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
    "required": ["steps"],
}
COOK = {
    "name": "cook",
    "kind": "stage",
    "server": "uvx cook-mcp",
    "stage_types": ["connector"],
    "types": {"recipe": RECIPE},
    "input_types": ["recipe"],
}


def recipe_flow(output_type: str = "output_type: recipe") -> str:
    return f"""name: Soup
stages:
  - swarm: write
    goal: Write a soup recipe.
    done_when: A recipe.
    {output_type}
    produces: [recipe.md]
  - connector: cook it
    calls: cook
    input: recipe.md
"""


PLAN = [Answer({"tasks": [{"title": "Write it."}]})]
WORK = [
    Call(tool_id("query_graph"), {"question": "What did we cook?"}),
    Call(tool_id("claim_task")),
    Call(tool_id("write_artifact"), {"name": "recipe.md", "content": "Boil it, I guess."}),
    Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."}),
    Say("Done."),
]


def judged(fields: dict[str, Any], body: str = "Boil, then season.") -> list:
    item = {"title": "Soup", "body": body, "rank": 1, "sources": [], "fields": fields}
    return [Answer({"passed": True, "reasons": ["It is one."], "items": [item]})]


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def world_with_cook(client: TestClient, cook: dict[str, Any] = COOK) -> str:
    world = client.post("/api/worlds", json={"name": "Kitchen"}).json()["id"]
    added = client.post(f"/api/worlds/{world}/connectors", json=cook)
    assert added.status_code == 201, added.json()
    return world


def test_the_check_refuses_a_typed_input_its_swarm_stage_does_not_promise(
    running: TestClient,
) -> None:
    world = world_with_cook(running)
    (cook,) = [
        c for c in running.get(f"/api/worlds/{world}/connectors").json() if c["name"] == "cook"
    ]
    assert cook["types"] == {"recipe": RECIPE} and cook["input_types"] == ["recipe"]

    check = f"/api/worlds/{world}/workflows/check"
    found = running.post(check, json={"yaml": recipe_flow("")}).json()["violations"]
    assert [(v["rule"], v["stage"]) for v in found] == [("output_type", "cook it")]
    assert "from swarm stage 'write' as type recipe" in found[0]["message"]
    wrong = running.post(check, json={"yaml": recipe_flow("output_type: script")}).json()
    assert "declares output_type script" in wrong["violations"][0]["message"]
    assert running.post(check, json={"yaml": recipe_flow()}).json()["violations"] == []

    bad = {**COOK, "name": "bad_cook", "types": {"recipe": {"type": "soup"}}}
    refused = running.post(f"/api/worlds/{world}/connectors", json=bad)
    assert refused.status_code == 422 and "not a JSON Schema" in refused.json()["detail"]


def test_a_run_start_rechecks_the_contract_at_the_versions_it_recorded(
    running: TestClient,
) -> None:
    untyped = {k: v for k, v in COOK.items() if k not in ("types", "input_types")}
    world = world_with_cook(running, untyped)
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": recipe_flow("")})
    assert saved.status_code == 201, saved.json()
    workflow = saved.json()["workflow_id"]
    assert running.delete(f"/api/worlds/{world}/packages/cook").status_code == 204
    assert running.post(f"/api/worlds/{world}/connectors", json=COOK).status_code == 201

    # The run uses the untyped cook it was saved with (#223), so its check passes; a new
    # version, on the typed cook the world pins now, does not.
    again = running.post(f"/api/workflows/{workflow}/versions", json={"yaml": recipe_flow("")})
    assert [v["rule"] for v in again.json()["violations"]] == ["output_type"]
    started = running.post(f"/api/workflows/{workflow}/runs", json={})
    assert started.status_code == 201, started.json()
    running.post(f"/api/runs/{started.json()['id']}/abort").raise_for_status()

    assert running.delete(f"/api/worlds/{world}/packages/cook").status_code == 204
    gone = running.post(f"/api/workflows/{workflow}/runs", json={})
    assert gone.status_code == 409, gone.json()
    assert [v["rule"] for v in gone.json()["violations"]] == ["unknown_connector"]


def test_a_promised_type_is_projected_in_its_schema_and_reaches_the_connector(
    running: TestClient, script: Script, transport: FakeTransport, tmp_path: Path
) -> None:
    script.scripts = [PLAN, WORK, judged({"steps": ["Boil.", "Season."]})]
    world = world_with_cook(running)
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": recipe_flow()}).json()
    run = running.post(f"/api/workflows/{saved['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    ended = running.get(f"/api/runs/{run['id']}").json()
    assert ended["status"] == "done", ended

    judge = script.sessions[2]
    item = (judge.output_format or {})["schema"]["properties"]["items"]["items"]
    assert item["properties"]["fields"] == RECIPE
    write = ended["stages"][0]["id"]
    recipe = tmp_path / "worlds" / world / "runs" / run["id"] / write / "artifacts" / "recipe.md"
    text = recipe.read_text()
    assert 'type: "recipe"' in text or "type: recipe" in text
    fenced = json.loads(text.split("```json\n")[1].split("\n```")[0])
    assert fenced == {"steps": ["Boil.", "Season."]} and "Boil it, I guess." not in text
    ((name, _, args, _),) = [c for c in transport.calls if c[0] == "cook"]
    assert args["input_path"] == str(recipe)


# World-dependent types (issue 140): the fixture cook answers its recipe for a world, with the
# pots that world has as an enum. The core passes the schema on and never reads it.
POTS = {
    **RECIPE,
    "properties": {**RECIPE["properties"], "pot": {"type": "string", "enum": ["wok", "stockpot"]}},
}
WORLD_COOK = {**COOK, "world_types": "recipe_types"}


def test_the_judge_projects_the_type_as_the_connector_answers_it_for_the_world(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    transport.outputs["recipe_types"] = [{"types": {"recipe": POTS}}]
    script.scripts = [PLAN, WORK, judged({"steps": ["Boil."], "pot": "wok"})]
    world = world_with_cook(running, WORLD_COOK)
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": recipe_flow()}).json()
    run = running.post(f"/api/workflows/{saved['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    assert running.get(f"/api/runs/{run['id']}").json()["status"] == "done"
    item = (script.sessions[2].output_format or {})["schema"]["properties"]["items"]["items"]
    assert item["properties"]["fields"] == POTS
    asked = [c for c in transport.calls if c[1] == "recipe_types"]
    assert asked and all(c[2]["world"] == world for c in asked)


def test_a_connector_that_cannot_answer_for_the_world_falls_back_to_its_declared_type(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    transport.outputs["recipe_types"] = [ConnectorError("cook: could not start")]
    script.scripts = [PLAN, WORK, judged({"steps": ["Boil."]})]
    world = world_with_cook(running, WORLD_COOK)
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": recipe_flow()}).json()
    run = running.post(f"/api/workflows/{saved['workflow_id']}/runs", json={}).json()
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    assert running.get(f"/api/runs/{run['id']}").json()["status"] == "done"
    item = (script.sessions[2].output_format or {})["schema"]["properties"]["items"]["items"]
    assert item["properties"]["fields"] == RECIPE


def test_the_check_and_the_run_start_compare_the_types_as_answered_for_the_world(
    running: TestClient, transport: FakeTransport
) -> None:
    transport.outputs["recipe_types"] = [{"types": {"recipe": RECIPE}}]
    world = world_with_cook(running, WORLD_COOK)
    bake = {**COOK, "name": "bake"}  # declares the same recipe, never world-dependent
    assert running.post(f"/api/worlds/{world}/connectors", json=bake).status_code == 201
    check = f"/api/worlds/{world}/workflows/check"
    untyped = "name: Plain\nstages:\n  - swarm: s\n    goal: g\n    done_when: d\n"
    assert running.post(check, json={"yaml": untyped}).json()["violations"] == []
    assert not [c for c in transport.calls if c[1] == "recipe_types"]  # nothing promised
    saved = running.post(f"/api/worlds/{world}/workflows", json={"yaml": recipe_flow()})
    assert saved.status_code == 201, saved.json()

    transport.outputs["recipe_types"] = [{"types": {"recipe": POTS}}]  # the world changed
    found = running.post(check, json={"yaml": recipe_flow()}).json()["violations"]
    assert [v["rule"] for v in found] == ["type_conflict"]
    started = running.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={})
    assert started.status_code == 409
    assert [v["rule"] for v in started.json()["violations"]] == ["type_conflict"]


# The speech connector voicing a projected script, then remotion_build, all real but the model.


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(seconds * 16000))
    return buf.getvalue()


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
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SPEECH_ENDPOINTS", f"http://127.0.0.1:{server.server_port}")
    yield
    server.shutdown()
    thread.join(timeout=5)


SHORT = """name: Typed short
stages:
  - swarm: script
    goal: Write a two scene script.
    done_when: A script.
    output_type: script
    produces: [script.md]
  - connector: voice
    calls: speech
    input: script.md
    output: timings.json
  - connector: video
    calls: remotion_build
    inputs: [script.md, timings.json]
"""
SCENES = {
    "scenes": [
        {"id": "hook", "lines": [{"text": "Who sleeps all winter?", "pause_after": 0.4}]},
        {"id": "fact", "lines": [{"text": "(whispers) A hedgehog.", "pause_after": 0.3}]},
    ]
}


@pytest.mark.skipif(
    not (shutil.which("sandbox-exec") or shutil.which("bwrap")), reason="no OS sandbox"
)
def test_speech_voices_the_projected_script_and_the_build_reads_both(
    tmp_path: Path,
    fake_cli: None,  # noqa: F811
    engine: None,
    opened: list[TestClient],  # noqa: F811
) -> None:
    builder = [
        Call("Bash", {"command": "npm install"}),
        Answer(_installs),
        Call("Write", {"file_path": "src/Main.tsx", "content": "export const Main = 1"}),
        Say("Built."),
    ]
    script = Script()
    script.scripts = [PLAN, WORK, judged(SCENES, "Who sleeps all winter? A hedgehog."), builder]
    data = tmp_path / "data"
    app = create_app(
        data,
        poll_seconds=0.05,
        retro=False,
        runtime=SdkRuntime(transport=script.transport),
        extract=fake_extract,
    )
    client = TestClient(app)
    client.__enter__()
    opened.append(client)
    world = client.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SHORT})
    assert saved.status_code == 201, saved.json()
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    ended = settled(client, run["id"])
    assert ended["status"] == "done", ended

    stage = {s["name"]: s["id"] for s in ended["stages"]}
    folder = data / "worlds" / world / "runs" / run["id"]
    projected = (folder / stage["script"] / "artifacts" / "script.md").read_text()
    assert json.loads(projected.split("```json\n")[1].split("\n```")[0]) == SCENES
    timings = json.loads((folder / stage["voice"] / "artifacts" / "timings.json").read_text())
    assert [t["file"] for t in timings["takes"]] == ["narration/hook.wav", "narration/fact.wav"]
    assert timings["takes"][1]["lines"][0]["directions"] == ["whispering"]
    assert "timings.json" in script.prompts[3] and "script.md" in script.prompts[3]
