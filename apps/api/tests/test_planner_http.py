"""The planner chat over HTTP: a real Agent SDK session with the scripted CLI behind it, whose
tools are the swarmify MCP server's own, called in-process."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
