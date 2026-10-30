"""The planner chat over HTTP: a real Agent SDK session with the scripted CLI behind it, whose
tools are the swarmify MCP server's own, called in-process."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import FakeTransport
from swarmify_swarm import tool_id
from swarmify_swarm.fake import Answer, Call, Die, Say, Script

DRAFT = """name: Weekly short
schedule: "0 8 * * 1"
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    tools: [web_search, graph]
  - gate: pick
    ask: Pick one idea.
    kind: pick
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _world(client: TestClient) -> str:
    return client.post("/api/worlds", json={"name": "Video"}).json()["id"]


def test_the_planner_checks_its_draft_through_the_mcp_tool_and_returns_the_yaml(
    running: TestClient, script: Script
) -> None:
    world = _world(running)
    script.scripts = [
        [
            Call(tool_id("check_workflow"), {"world_id": world, "yaml": DRAFT}),
            Call("Bash", {"command": "rm -rf /"}),
            Answer({"reply": "Here is a weekly short: research, then you pick.", "yaml": DRAFT}),
        ]
    ]
    answered = running.post(
        f"/api/worlds/{world}/planner", json={"message": "A weekly short with a pick gate."}
    )
    assert answered.status_code == 200, answered.text
    reply = answered.json()
    assert reply["reply"].startswith("Here is a weekly short")
    assert reply["yaml"] == DRAFT and reply["saved"] is None
    assert reply["session_id"].startswith("session-")

    # the tool call reached the real check_workflow: the fake CLI saw its answer
    checked, denied = script.outcomes
    assert checked.tool == tool_id("check_workflow") and not checked.is_error
    assert '"violations": []' in checked.text and '"name": "Weekly short"' in checked.text
    assert denied.is_error and "not allowed in the planner chat" in denied.text
    assert "A weekly short with a pick gate." in script.prompts[0]
    assert world in script.sessions[0].system_prompt  # type: ignore[operator]
    assert running.get(f"/api/worlds/{world}/workflows").json() == []


def test_a_follow_up_resumes_the_session_and_a_save_comes_back_as_the_version(
    running: TestClient, script: Script
) -> None:
    world = _world(running)
    script.scripts = [
        [Answer({"reply": "Drafted.", "yaml": DRAFT})],
        [
            Call(tool_id("save_workflow"), {"world_id": world, "yaml": DRAFT}),
            Answer({"reply": "Saved as version 1.", "yaml": None}),
        ],
    ]
    first = running.post(f"/api/worlds/{world}/planner", json={"message": "Draft it."}).json()
    second = running.post(
        f"/api/worlds/{world}/planner",
        json={"message": "Save it.", "yaml": first["yaml"], "session_id": first["session_id"]},
    )
    assert second.status_code == 200, second.text
    reply = second.json()
    assert reply["session_id"] == first["session_id"]
    assert script.sessions[1].resume == first["session_id"]
    assert "```yaml" in script.prompts[1] and "Weekly short" in script.prompts[1]
    saved = reply["saved"]
    assert saved and saved["number"] == 1 and saved["model"]["name"] == "Weekly short"
    assert reply["yaml"] == DRAFT  # the saved text stands in when the planner returns none
    [listed] = running.get(f"/api/worlds/{world}/workflows").json()
    assert listed["id"] == saved["workflow_id"]


def test_a_planner_that_dies_or_answers_badly_is_a_502_the_operator_can_retry(
    running: TestClient, script: Script
) -> None:
    world = _world(running)
    script.scripts = [[Die()], [Say("no json")], [Answer({"reply": "Fine."})]]
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "x"}).status_code == 502
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "x"}).status_code == 502
    ok = running.post(f"/api/worlds/{world}/planner", json={"message": "x"})
    assert ok.status_code == 200 and ok.json()["yaml"] is None
    assert running.post("/api/worlds/world_nope/planner", json={"message": "x"}).status_code == 404
    assert running.post(f"/api/worlds/{world}/planner", json={"message": ""}).status_code == 422


def test_the_planner_chat_runs_on_the_world_model(running: TestClient, script: Script) -> None:
    world = _world(running)
    script.scripts = [[Answer({"reply": "Hi.", "yaml": None})]]
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "Hi"}).is_success
    running.patch(f"/api/worlds/{world}", json={"model": "claude-sonnet-5-5"})
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "Hi"}).is_success
    assert [o.model for o in script.sessions] == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_the_planner_sees_the_worlds_typed_inputs(running: TestClient, script: Script) -> None:
    world = _world(running)
    cook = {"name": "cook", "kind": "stage", "server": "uvx cook-mcp"}
    cook |= {"stage_types": ["connector"], "types": {"recipe": {"type": "object"}}}
    added = running.post(
        f"/api/worlds/{world}/connectors", json={**cook, "input_types": ["recipe"]}
    )
    assert added.status_code == 201, added.text
    script.scripts = [[Answer({"reply": "Hi.", "yaml": None})]]
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "Hi"}).is_success
    system = script.sessions[0].system_prompt or ""
    assert "must declare that type as its output_type" in system
    assert "- speech: input 1 is type script" in system
    assert "- cook: input 1 is type recipe" in system


def test_the_planner_hears_what_a_connector_says_of_its_types_in_this_world(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    world = _world(running)
    post = {"name": "poster", "kind": "stage", "server": "uvx poster-mcp"}
    post |= {"stage_types": ["connector"], "types": {"post": {"type": "object"}}}
    post |= {"input_types": ["post"], "world_types": "post_types"}
    assert running.post(f"/api/worlds/{world}/connectors", json=post).status_code == 201
    transport.outputs["post_types"] = [{"types": {}, "note": "This world posts to news, kids."}]
    script.scripts = [[Answer({"reply": "Hi.", "yaml": None})]]
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "Hi"}).is_success
    system = script.sessions[0].system_prompt or ""
    assert "- poster: This world posts to news, kids." in system


def test_the_planner_describes_each_connector_from_its_manifest(
    running: TestClient, script: Script
) -> None:
    from swarmify_kernel import AGENT_TOOLS

    world = _world(running)
    cook = {"name": "cook", "kind": "tool", "server": "uvx cook-mcp", "stage_types": ["swarm"]}
    assert running.post(f"/api/worlds/{world}/connectors", json=cook).status_code == 201
    script.scripts = [[Answer({"reply": "Hi.", "yaml": None})]]
    assert running.post(f"/api/worlds/{world}/planner", json={"message": "Hi"}).is_success
    system = str(script.sessions[0].system_prompt)
    speech = system.split("\n- speech (")[1].split("\n- ")[0]  # a default package
    assert all(f"params.{p}" in speech for p in ("voice", "engines", "seed"))
    posts = system.split("\n- upload_post (")[1].split("\n- ")[0]
    assert all(f"params.{p}" in posts for p in ("title", "schedule", "user", "platforms"))
    youtube = system.split("\n- youtube (")[1].split("\n- ")[0]
    published = ("title", "metadata", "description", "visibility", "schedule", "made_for_kids")
    assert all(f"params.{p}" in youtube for p in published)
    render = system.split("\n- remotion_render (")[1].split("\n- ")[0]
    assert "only when the operator has a finished template" in render
    for param in ("project", "composition", "entry", "codec", "frames", "props"):
        assert f"params.{param}" in render, param
    built = system.split("\n- remotion_build (")[1].split("\n- ")[0]
    for param in ("base", "composition", "brief", "model", "max_turns", "minutes"):
        assert f"params.{param}" in built, param
    # what the hand-written prompt said moved into each connector's own notes
    assert "This is how a workflow makes a video" in system
    assert "(pause 1.5s) is silence" in system and "params.user: required;" in system
    assert "- cook (tool, in swarm stages)" in system  # a world's own, without a Manifest
    assert "youtube, upload_post" not in system and "agents write text only" in system
    agent_tools = system.split("The Agent Tools")[1].split("How you work")[0]
    assert all(f"- {name}: " in agent_tools for name in AGENT_TOOLS)


def test_a_package_s_planner_notes_and_params_reach_the_planner() -> None:
    from pathlib import Path

    from swarmify_api.planner import describe
    from swarmify_connectors import Connector, read_manifest

    echo = Path(__file__).parents[3] / "packages/connectors/tests/fixtures/packages/echo"
    m = read_manifest(echo)
    told = describe(Connector("p", "w", "echo", "stage", stage_types=["connector"], manifest=m))
    assert told.splitlines() == [
        "- echo (stage, in connector stages): Says back what it is given, as audio. A fixture.",
        "  input: the line to say (text/markdown)",
        "  output: the line, said (audio/wav)",
        "  params.loud: Say it louder",
        "  Use echo to voice a short line.",
    ]


def test_the_planner_starts_a_run_when_the_operator_asks(
    running: TestClient, script: Script
) -> None:
    world = _world(running)
    wf = running.post(f"/api/worlds/{world}/workflows", json={"yaml": DRAFT}).json()
    script.scripts = [
        [
            Call(tool_id("start_run"), {"workflow_id": wf["workflow_id"], "budget_usd": 2}),
            Answer({"reply": "Started.", "yaml": None}),
        ],
        [],  # the run's own planner session dies, failing the run: nothing else to script
    ]
    answered = running.post(f"/api/worlds/{world}/planner", json={"message": "Run it."})
    assert answered.status_code == 200, answered.text
    (started,) = script.outcomes
    assert not started.is_error, started.text
    (run,) = running.get(f"/api/worlds/{world}/runs").json()
    assert (run["workflow_id"], run["budget_usd"]) == (wf["workflow_id"], 2)
