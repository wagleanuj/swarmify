"""Rerun and Skip over HTTP (ADR 0027): a Rerun from a gate runs the stage again with its note,
a Skip passes over a stage that has not started, and the workflow check refuses a Skip that
leaves a later stage without its input or a public post without its gate. Real DBOS on SQLite,
the fake ConnectorTransport behind the stage calls."""

from collections.abc import Iterator
from typing import Any

import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import FakeTransport

MAKER = {"name": "maker", "kind": "stage", "server": "uvx maker-mcp", "stage_types": ["connector"]}

POST = """name: Render then post
stages:
  - connector: render
    calls: maker
    output: short.mp4
  - gate: check
    ask: Good?
  - connector: cut
    calls: maker
    output: cut.mp4
  - connector: tag
    calls: maker
    input: cut.mp4
  - gate: confirm
    ask: Post it?
    kind: confirm
  - connector: publish
    calls: youtube
    input: short.mp4
    params:
      title: Five hooks that work
      visibility: public
"""


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def at_check(client: TestClient, yaml: str = POST) -> dict[str, Any]:
    world = client.post("/api/worlds", json={"name": "Video"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=MAKER).status_code == 201
    saved = client.post(f"/api/worlds/{world}/workflows", json={"yaml": yaml})
    assert saved.status_code == 201, saved.text
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    return client.get(f"/api/runs/{run['id']}").json()


def step(run: dict[str, Any], kind: str, name: str, **payload: Any) -> dict[str, Any]:
    target = next(s for s in run["stages"] if s["name"] == name)
    return {"kind": kind, "stage": target["id"], "payload": payload}


def apply(client: TestClient, run: dict[str, Any], change: dict[str, Any]) -> Any:
    proposal = client.post(f"/api/runs/{run['id']}/changes/propose", json=change)
    assert proposal.status_code == 200, proposal.text
    return client.post(
        f"/api/runs/{run['id']}/changes", json={**change, "revision": proposal.json()["revision"]}
    )


def test_a_rerun_from_a_gate_runs_the_stage_again_with_its_note(
    running: TestClient, transport: FakeTransport
) -> None:
    run = at_check(running)
    rerun = step(run, "rerun", "render", note="Louder music.")

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=rerun).json()
    assert proposal["before"] == {"render": "done", "check": "waiting"}
    assert proposal["after"] == {"render": "pending", "check": "pending"}
    assert proposal["violations"] == [] and proposal["cost_usd"] == 0.0
    applied = apply(running, run, rerun)
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"] == {"note": "Louder music."}

    def renders() -> int:  # a visit after the first is keyed `<stage id>.<visit>`
        return sum(1 for c in transport.calls if rerun["stage"] in c[2].get("idempotency_key", ""))

    until(lambda: renders() == 2)
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    after = running.get(f"/api/runs/{run['id']}").json()
    assert after["stages"][0]["notes"] == ["Louder music."]
    assert [c["kind"] for c in running.get(f"/api/runs/{run['id']}/changes").json()] == ["rerun"]

    pending = step(run, "rerun", "cut")
    refused = apply(running, after, pending)
    assert refused.status_code == 409 and refused.json()["detail"] == "cut has not finished"


def test_a_skip_passes_over_a_stage_unless_a_later_one_needs_it_or_it_gates_a_public_post(
    running: TestClient, transport: FakeTransport
) -> None:
    run = at_check(running)
    for name, refused in [
        ("cut", "input 'cut.mp4' is not produced by an earlier stage"),
        ("confirm", "publish connector 'youtube' must come right after a gate"),
    ]:
        skipped = apply(running, run, step(run, "skip", name))
        assert skipped.status_code == 422 and refused in skipped.json()["detail"]

    skip = step(run, "skip", "tag")
    assert apply(running, run, skip).status_code == 201
    assert running.post(f"/api/runs/{run['id']}/decision", json={}).is_success
    until(lambda: running.get(f"/api/runs/{run['id']}").json()["stages"][4]["status"] == "waiting")
    after = running.get(f"/api/runs/{run['id']}").json()
    assert [s["status"] for s in after["stages"][2:5]] == ["done", "skipped", "waiting"]
    assert not [c for c in transport.calls if skip["stage"] in c[2].get("idempotency_key", "")]


def test_the_gate_before_a_private_post_may_be_skipped(running: TestClient) -> None:
    run = at_check(running, POST.replace("visibility: public", "visibility: private"))
    skip = step(run, "skip", "confirm")
    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=skip).json()
    assert proposal["violations"] == []
