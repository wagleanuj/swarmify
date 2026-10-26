"""Workflow stages over HTTP: real DBOS on SQLite, a scripted agent CLI in the grandchild, and
artifacts copied up two levels across worlds."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_swarm import tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

LEAF = """name: Trends
stages:
  - swarm: research
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    produces: [ideas.md]
"""
SCRIPT = [
    Call(tool_id("query_graph"), {"question": "What did we reject before?"}),
    Call(tool_id("claim_task")),
    Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"}),
    Call(tool_id("finish_task"), {"task_id": LAST, "note": "Five found."}),
    Say("Five ideas."),
]
PLAN = [Answer({"tasks": [{"title": "Find 5 ideas trending this week."}]})]
PASS = [Answer({"passed": True, "reasons": ["Five ideas."]})]


def runs(name: str, child: str, then: str) -> str:
    return (
        f"name: {name}\nstages:\n  - workflow: sub\n    runs: {child}\n    take: [ideas.md]\n{then}"
    )


PICK = "  - gate: pick\n    ask: Pick one.\n    kind: pick\n"
REVIEW = "  - gate: review\n    ask: Approve?\n"


@pytest.fixture
def running(app: FastAPI, script: Script) -> Iterator[TestClient]:
    script.scripts, script.cost, script.cycle = [PLAN, SCRIPT, PASS], 0.042, True
    with TestClient(app) as client:
        yield client


def save(client: TestClient, world: str, yaml: str) -> dict:
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 201, saved.json()
    return saved.json()


def three_levels(client: TestClient) -> tuple[str, str, str, str]:
    """Top (world Video) runs Mid (world Research), which runs Trends (world Research)."""
    video = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    research = client.post("/api/worlds", json={"name": "Research"}).json()["id"]
    leaf = save(client, research, LEAF)["workflow_id"]
    mid = save(client, research, runs("Mid", leaf, PICK))["workflow_id"]
    top = save(client, video, runs("Top", mid, REVIEW))["workflow_id"]
    return video, research, top, leaf


def get(client: TestClient, run_id: str) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


def child(client: TestClient, run_id: str) -> dict:
    until(lambda: get(client, run_id)["stages"][0]["child"] is not None)
    return get(client, get(client, run_id)["stages"][0]["child"]["id"])


def test_depth_two_takes_the_grandchilds_artifact_up_across_worlds(
    running: TestClient, tmp_path: Path
) -> None:
    video, research, top_wf, _ = three_levels(running)
    top = running.post(f"/api/workflows/{top_wf}/runs", json={}).json()
    mid = child(running, top["id"])
    leaf = child(running, mid["id"])
    assert (mid["world_id"], mid["name"], mid["parent"]["name"]) == (research, "Mid", "Top")
    assert mid["parent"]["world_id"] == video and leaf["parent"]["id"] == mid["id"]

    until(lambda: get(running, mid["id"])["status"] == "waiting_gate")
    assert get(running, leaf["id"])["status"] == "done"
    (gate,) = running.get("/api/gates").json()
    assert gate["run_id"] == mid["id"]
    assert gate["parent"] == {"id": top["id"], "world_id": video, "name": "Top"}
    listed = running.get(f"/api/worlds/{research}/runs").json()
    assert {r["name"]: (r["parent"] or {}).get("name") for r in listed} == {
        "Trends": "Mid",
        "Mid": "Top",
    }

    agents = running.get(f"/api/runs/{top['id']}/agents").json()
    assert [(a["run_id"], a["cost_usd"]) for a in agents] == [(leaf["id"], 0.042)] * 3, (
        "the grandchild's planner, worker and judge spend counts against the top run"
    )

    assert running.post(f"/api/runs/{mid['id']}/decision", json={"choice": "1"}).status_code == 200
    until(lambda: get(running, top["id"])["status"] == "waiting_gate")
    ended = get(running, top["id"])
    assert ended["stages"][0]["status"] == "done"
    for world, run in ((video, ended), (research, get(running, mid["id"]))):
        stage = run["stages"][0]["id"]
        copied = tmp_path / "worlds" / world / "runs" / run["id"] / stage / "artifacts/ideas.md"
        assert copied.read_text().endswith("1. Hooks")


def test_abort_cascades_down_two_levels(running: TestClient) -> None:
    _, _, top_wf, _ = three_levels(running)
    top = running.post(f"/api/workflows/{top_wf}/runs", json={}).json()
    mid = child(running, top["id"])
    until(lambda: get(running, mid["id"])["status"] == "waiting_gate")
    assert running.post(f"/api/runs/{top['id']}/abort").status_code == 200
    until(lambda: get(running, top["id"])["status"] == "aborted")
    until(lambda: get(running, mid["id"])["status"] == "aborted")
    assert running.get("/api/gates").json() == []


def test_saving_a_cycle_or_a_third_level_is_refused(client: TestClient) -> None:
    video, research, top_wf, leaf_wf = three_levels(client)
    loop = runs("Trends", top_wf, "")
    refused = client.post(f"/api/workflows/{leaf_wf}/versions", json={"yaml": loop})
    assert refused.status_code == 422
    assert [v["rule"] for v in refused.json()["violations"]] == ["cycle"]

    deeper = client.post(f"/api/worlds/{video}/workflows", json={"yaml": runs("Over", top_wf, "")})
    assert deeper.status_code == 422
    assert [v["rule"] for v in deeper.json()["violations"]] == ["depth"]
    assert [w["name"] for w in client.get(f"/api/worlds/{video}/workflows").json()] == ["Top"]
