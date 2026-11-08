"""Run Changes over HTTP (ADR 0027), Stage Edit first: propose shows the edit and the workflow
check at the run's pinned versions, apply writes it and records who, a stale revision is refused,
and every applied change is listed and streamed. Real DBOS on SQLite, the fake ConnectorTransport
behind the stage calls."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_connectors import ConnectorError, FakeTransport

RENDER = {
    "name": "render_video",
    "kind": "stage",
    "server": "uvx render-mcp",
    "stage_types": ["connector"],
}

SHORT = """name: Pick then render
stages:
  - gate: pick
    ask: Which idea?
    kind: pick
  - connector: render
    calls: render_video
    output: short.mp4
    params:
      fps: 30
  - connector: tag
    calls: render_video
  - connector: render_video
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def at_gate(client: httpx.Client | TestClient) -> dict[str, Any]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=RENDER).status_code == 201
    workflow = client.post(f"/api/worlds/{world}/workflows", json={"yaml": SHORT}).json()
    run = client.post(f"/api/workflows/{workflow['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    return client.get(f"/api/runs/{run['id']}").json()


def stage(run: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in run["stages"] if s["name"] == name)


def change(run: dict[str, Any], name: str, **config: Any) -> dict[str, Any]:
    edited = {**stage(run, name)["config"], **config}
    return {"kind": "stage_edit", "stage": stage(run, name)["id"], "payload": edited}


def test_a_stage_edit_is_proposed_applied_listed_and_streamed_and_the_stage_runs_with_it(
    live_url: str, transport: FakeTransport
) -> None:
    client = httpx.Client(base_url=live_url, timeout=10)
    run = at_gate(client)
    edit = change(run, "render", params={"fps": 60})

    proposed = client.post(f"/api/runs/{run['id']}/changes/propose", json=edit)
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()
    assert proposal["before"]["params"] == {"fps": 30}
    assert proposal["after"]["params"] == {"fps": 60}
    assert proposal["violations"] == [] and proposal["in_place"] is True
    assert proposal["revision"] == run["revision"]
    assert client.get(f"/api/runs/{run['id']}/changes").json() == []

    applied = client.post(
        f"/api/runs/{run['id']}/changes", json={**edit, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["by"] == "operator" and applied.json()["stage_name"] == "render"
    listed = client.get(f"/api/runs/{run['id']}/changes").json()
    assert [(c["kind"], c["payload"]["params"]) for c in listed] == [("stage_edit", {"fps": 60})]
    after = client.get(f"/api/runs/{run['id']}").json()
    assert after["changes"] == listed and after["revision"] > run["revision"]

    with httpx.stream(
        "GET", f"{live_url}/api/worlds/{run['world_id']}/events?since=0", timeout=10
    ) as r:
        changed = next(data for kind, _, data in sse_events(r) if kind == "run.changed")
    assert changed["run_id"] == run["id"]
    assert changed["payload"]["id"] == applied.json()["id"]

    decided = client.post(f"/api/runs/{run['id']}/decision", json={"choice": "idea 1"})
    assert decided.status_code == 200, decided.text
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    render = next(c for c in transport.calls if c[2].get("idempotency_key") == edit["stage"])
    assert render[2]["fps"] == 60


def test_a_partial_stage_edit_is_stored_with_the_defaults_a_workflow_fills_in(
    running: TestClient, transport: FakeTransport
) -> None:
    run = at_gate(running)
    target = stage(run, "render_video")
    edit = {"kind": "stage_edit", "stage": target["id"], "payload": {"params": {"fps": 24}}}

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=edit).json()
    assert proposal["violations"] == [] and proposal["after"]["calls"] == "render_video"
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**edit, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"] == proposal["after"]

    assert running.post(f"/api/runs/{run['id']}/decision", json={"choice": "idea 1"}).is_success
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    call = next(c for c in transport.calls if c[2].get("idempotency_key") == target["id"])
    assert call[2]["fps"] == 24


@pytest.mark.parametrize(
    ("config", "refused"),
    [
        (
            {"calls": "youtube", "input": "short.mp4", "params": {"title": "Hooks",
             "visibility": "public"}},
            "publish connector 'youtube' must come right after a gate",
        ),
        ({"input": "nothing.md"}, "input 'nothing.md' is not produced by an earlier stage"),
        ({"calls": "nobody"}, "no connector named 'nobody'"),
    ],
)  # fmt: skip
def test_a_stage_edit_that_breaks_a_workflow_rule_is_refused_at_propose_and_at_apply(
    running: TestClient, config: dict[str, Any], refused: str
) -> None:
    run = at_gate(running)
    edit = change(run, "tag", **config)

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=edit).json()
    assert any(refused in v for v in proposal["violations"]), proposal["violations"]
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**edit, "revision": proposal["revision"]}
    )
    assert applied.status_code == 422 and refused in applied.json()["detail"]
    assert running.get(f"/api/runs/{run['id']}/changes").json() == []


def test_a_stale_revision_or_a_started_stage_is_refused(running: TestClient) -> None:
    run = at_gate(running)
    edit = change(run, "render", params={"fps": 60})
    revision = running.post(f"/api/runs/{run['id']}/changes/propose", json=edit).json()["revision"]
    first = running.post(f"/api/runs/{run['id']}/changes", json={**edit, "revision": revision})
    assert first.status_code == 201

    again = running.post(f"/api/runs/{run['id']}/changes", json={**edit, "revision": revision})
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "The run moved on since this was proposed: render was edited by operator."
    )

    gate = change(run, "pick", ask="Which one?")
    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=gate).json()
    assert proposal["violations"] == [
        "pick is waiting; only a stage that has not started can be edited"
    ]
    assert running.post("/api/runs/run_nope/changes/propose", json=gate).status_code == 404
    other = {**gate, "kind": "restructure"}
    assert running.post(f"/api/runs/{run['id']}/changes/propose", json=other).status_code == 422
    bare = {"kind": "stage_edit", "stage": gate["stage"]}
    assert running.post(f"/api/runs/{run['id']}/changes/propose", json=bare).status_code == 422


def restructure(run: dict[str, Any], name: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "restructure", "stage": stage(run, name)["id"], "payload": {"stages": stages}}


def shape(run: dict[str, Any], name: str) -> dict[str, Any]:
    s = stage(run, name)
    return {"name": s["name"], "type": s["type"], **s["config"]}


def test_a_restructure_answers_with_the_fork_it_continues_in_and_ends_the_run(
    running: TestClient,
) -> None:
    run = at_gate(running)
    added = {"name": "thumb", "type": "connector", "calls": "render_video"}
    body = restructure(run, "tag", [shape(run, "tag"), added, shape(run, "render_video")])

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=body).json()
    assert proposal["violations"] == [] and proposal["in_place"] is False
    assert proposal["fork_from"] == "pick"
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**body, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    fork_id = applied.json()["fork"]["id"]

    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "aborted")
    original = running.get(f"/api/runs/{run['id']}").json()
    assert [f["id"] for f in original["forks"]] == [fork_id]
    fork = running.get(f"/api/runs/{fork_id}").json()
    assert fork["forked_from"]["id"] == run["id"]
    assert fork["forked_by"]["id"] == applied.json()["id"]
    names = ["pick", "render", "tag", "thumb", "render_video"]
    assert [s["name"] for s in fork["stages"]] == names


def test_a_restructure_that_breaks_a_workflow_rule_is_refused_with_the_rules_message(
    running: TestClient,
) -> None:
    run = at_gate(running)
    reads = {"name": "thumb", "type": "connector", "calls": "render_video", "input": "nothing.md"}
    body = restructure(run, "tag", [reads])
    refused = "input 'nothing.md' is not produced by an earlier stage"

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=body).json()
    assert any(refused in v for v in proposal["violations"]), proposal["violations"]
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**body, "revision": proposal["revision"]}
    )
    assert applied.status_code == 422 and refused in applied.json()["detail"]
    assert running.get(f"/api/runs/{run['id']}").json()["forks"] == []


def failed_at_the_end(client: TestClient, transport: FakeTransport) -> dict[str, Any]:
    """A run whose last stage, the upload, was refused; the same call in a Fork goes through.
    `render` leaves a caption beside its video."""
    run = at_gate(client)
    upload = stage(run, "render_video")

    def refused_once(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("output_path"):
            Path(arguments["output_path"]).with_name("caption.md").write_text("A caption.")
        if arguments.get("idempotency_key") == upload["id"]:
            raise ConnectorError("render_video: upload refused")
        return {"ok": True}

    transport.outputs["render_video"] = [refused_once]
    assert client.post(f"/api/runs/{run['id']}/decision", json={"choice": "idea 1"}).is_success
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "failed")
    return client.get(f"/api/runs/{run['id']}").json()


def test_a_rerun_of_a_failed_run_answers_with_the_fork_that_runs_only_that_stage_on(
    running: TestClient, transport: FakeTransport
) -> None:
    run = failed_at_the_end(running, transport)
    body = {"kind": "rerun", "stage": stage(run, "render_video")["id"], "payload": {"note": "Go."}}

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=body).json()
    assert proposal["violations"] == [] and proposal["in_place"] is False
    assert proposal["fork_from"] == "render_video"
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**body, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    fork_id = applied.json()["fork"]["id"]
    until(lambda: running.get(f"/api/runs/{fork_id}").json()["status"] == "done")
    calls = [c[2]["idempotency_key"] for c in transport.calls if c[1] == "render_video"]
    assert len(calls) == 4, "render and tag ran once; only the upload runs again in the fork"
    assert running.get(f"/api/runs/{run['id']}").json()["status"] == "failed"


def test_a_skip_or_an_artifact_edit_of_a_failed_run_answers_with_the_fork_it_continues_in(
    running: TestClient, transport: FakeTransport
) -> None:
    run = failed_at_the_end(running, transport)
    skip = {"kind": "skip", "stage": stage(run, "render_video")["id"], "payload": {}}
    payload = {"path": "caption.md", "text": "A better caption."}
    edit = {"kind": "artifact_edit", "stage": stage(run, "render")["id"], "payload": payload}

    for body in (skip, edit):
        proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=body).json()
        assert proposal["violations"] == [] and proposal["in_place"] is False, proposal
        applied = running.post(
            f"/api/runs/{run['id']}/changes", json={**body, "revision": proposal["revision"]}
        )
        assert applied.status_code == 201, applied.text
        assert applied.json()["fork"]["id"]
    assert running.get(f"/api/runs/{run['id']}").json()["status"] == "failed"
