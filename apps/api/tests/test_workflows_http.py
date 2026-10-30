import time
from datetime import UTC, datetime
from pathlib import Path

from api_fakes import FakeNotifier, until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_api.main import run_storage
from swarmify_learning.evals import TASKS, workflow_yaml
from swarmify_runs import RunControl

WEEKLY = """name: Weekly short
budget_usd: 5
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    tools: [web_search, graph]
  - gate: pick
    ask: Pick one.
    kind: pick
  - swarm: script
    goal: Write it.
    done_when: script.md written
    produces: [script.md]
  - connector: render
    calls: remotion_render
    input: script.md
    output: short.mp4
    params: {project: /tmp, composition: Short}
  - gate: review
    ask: Approve?
  - connector: youtube
    input: short.mp4
    params: {title: Five hooks}
"""


def _world(client: TestClient) -> str:
    return client.post("/api/worlds", json={"name": "Video"}).json()["id"]


def _workflow(client: TestClient, world_id: str, yaml: str = WEEKLY) -> str:
    created = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": yaml})
    assert created.status_code == 201, created.text
    return created.json()["workflow_id"]


def test_save_creates_versions_that_can_be_listed_and_fetched(client: TestClient) -> None:
    world_id = _world(client)
    assert client.get(f"/api/worlds/{world_id}/workflows").json() == []

    created = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": WEEKLY})
    assert created.status_code == 201, created.text
    v1 = created.json()
    assert v1["number"] == 1 and v1["yaml"] == WEEKLY and v1["id"].startswith("wfv_")
    assert v1["model"]["name"] == "Weekly short" and v1["model"]["budget_usd"] == 5
    assert [s["type"] for s in v1["model"]["stages"]] == [
        "swarm",
        "gate",
        "swarm",
        "connector",
        "gate",
        "connector",
    ]
    assert v1["model"]["stages"][3] == {
        "type": "connector",
        "name": "render",
        "calls": "remotion_render",
        "input": "script.md",
        "output": "short.mp4",
        "params": {"project": "/tmp", "composition": "Short"},
        "inputs": [],
    }

    wf = v1["workflow_id"]
    [listed] = client.get(f"/api/worlds/{world_id}/workflows").json()
    assert (
        listed["id"] == wf and listed["name"] == "Weekly short" and listed["current_version"] == 1
    )
    assert listed.pop("violations") == []  # its current version passes today's check
    assert client.get(f"/api/workflows/{wf}").json() == listed

    renamed = WEEKLY.replace("Weekly short", "Weekly short v2")
    v2 = client.post(f"/api/workflows/{wf}/versions", json={"yaml": renamed})
    assert v2.status_code == 201 and v2.json()["number"] == 2
    assert [v["number"] for v in client.get(f"/api/workflows/{wf}/versions").json()] == [1, 2]
    assert client.get(f"/api/workflows/{wf}/versions/1").json() == v1
    assert client.get(f"/api/workflows/{wf}/versions/2").json() == v2.json()
    assert client.get(f"/api/workflows/{wf}").json()["name"] == "Weekly short v2"
    assert client.get(f"/api/workflows/{wf}/versions/3").status_code == 404
    assert client.get(f"/api/workflows/{wf}/versions/abc").status_code == 422


def test_an_invalid_workflow_is_a_422_naming_every_violation_and_saves_nothing(
    client: TestClient,
) -> None:
    world_id = _world(client)
    bad = (
        "name: Bad\nstages:\n  - connector: youtube\n    input: short.mp4\n"
        "    params: {title: Hooks}\n"
    )
    refused = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": bad})
    assert refused.status_code == 422
    assert refused.json() == {
        "violations": [
            {
                "rule": "publish_without_gate",
                "message": "publish connector 'youtube' must come right after a gate",
                "stage": "youtube",
            },
            {
                "rule": "input_not_produced",
                "message": "input 'short.mp4' is not produced by an earlier stage",
                "stage": "youtube",
            },
        ],
        "model": None,
        "next_run_at": None,
    }
    assert client.get(f"/api/worlds/{world_id}/workflows").json() == []

    wf = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": WEEKLY}).json()[
        "workflow_id"
    ]
    assert client.post(f"/api/workflows/{wf}/versions", json={"yaml": bad}).status_code == 422
    assert len(client.get(f"/api/workflows/{wf}/versions").json()) == 1


def test_check_reports_inline_without_saving(client: TestClient) -> None:
    world_id = _world(client)
    url = f"/api/worlds/{world_id}/workflows/check"
    clean = client.post(url, json={"yaml": WEEKLY}).json()
    assert clean["violations"] == [] and clean["model"]["name"] == "Weekly short"

    broken = client.post(url, json={"yaml": "name: x\nstages:\n  - swarm: ["}).json()
    assert broken["model"] is None and broken["violations"][0]["rule"] == "yaml"

    typo = client.post(url, json={"yaml": "name: x\nstages:\n  - gate: g\n    asks: ok\n"}).json()
    assert [(v["rule"], v["stage"]) for v in typo["violations"]] == [
        ("schema", "g"),
        ("schema", "g"),
    ]
    assert client.get(f"/api/worlds/{world_id}/workflows").json() == []

    # a check for an existing workflow counts that workflow as itself, so self-reference is a cycle
    wf = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": WEEKLY}).json()[
        "workflow_id"
    ]
    loop = f"name: Loop\nstages:\n  - workflow: me\n    runs: {wf}\n"
    assert [
        v["rule"]
        for v in client.post(url, json={"yaml": loop, "workflow_id": wf}).json()["violations"]
    ] == ["cycle"]
    assert client.post(url, json={"yaml": loop}).json()["violations"] == []


def test_unknown_worlds_and_workflows_are_404s(client: TestClient) -> None:
    assert client.get("/api/worlds/world_nope/workflows").status_code == 404
    assert client.post("/api/worlds/world_nope/workflows", json={"yaml": WEEKLY}).status_code == 404
    assert (
        client.post("/api/worlds/world_nope/workflows/check", json={"yaml": WEEKLY}).status_code
        == 404
    )
    assert client.get("/api/workflows/wf_nope").status_code == 404
    assert client.get("/api/workflows/wf_nope/versions").status_code == 404
    assert client.get("/api/workflows/wf_nope/versions/1").status_code == 404
    assert client.post("/api/workflows/wf_nope/versions", json={"yaml": WEEKLY}).status_code == 404
    world_id = _world(client)
    check = client.post(
        f"/api/worlds/{world_id}/workflows/check", json={"yaml": WEEKLY, "workflow_id": "wf_nope"}
    )
    assert check.status_code == 404


def _runs(*ids: str) -> str:
    return "name: w\nstages:\n  - gate: g\n    ask: ok\n" + "".join(
        f"  - workflow: s{n}\n    runs: {wf}\n" for n, wf in enumerate(ids)
    )


def test_deleting_a_world_drops_its_workflows_from_every_rule(client: TestClient) -> None:
    a, b = _world(client), _world(client)
    child, leaf = _workflow(client, b, _runs()), _workflow(client, b, _runs())
    parent = _workflow(client, a, _runs(child))
    _workflow(client, a, _runs(parent))
    assert client.post(f"/api/workflows/{child}/versions", json={"yaml": _runs(leaf)}).json()[
        "violations"
    ] == [{"rule": "depth", "message": "workflows nest 3 deep, the limit is 2", "stage": None}]

    assert client.delete(f"/api/worlds/{a}").status_code == 204

    assert client.get(f"/api/workflows/{parent}").status_code == 404
    deeper = client.post(f"/api/workflows/{child}/versions", json={"yaml": _runs(leaf)})
    assert deeper.status_code == 201, deeper.text
    looped = client.post(f"/api/workflows/{leaf}/versions", json={"yaml": _runs(parent)})
    assert [v["rule"] for v in looped.json()["violations"]] == ["unknown_workflow"]


def test_saving_appears_on_the_world_event_stream(client: TestClient) -> None:
    world_id = _world(client)
    wf = client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": WEEKLY}).json()[
        "workflow_id"
    ]
    client.post(f"/api/workflows/{wf}/versions", json={"yaml": WEEKLY})
    with client.stream(
        "GET", f"/api/worlds/{world_id}/events", headers={"Last-Event-ID": "1"}
    ) as r:
        lines = []
        for line in r.iter_lines():
            lines.append(line)
            if len(lines) >= 10:  # the id-only head frame, then two events
                break
    assert "event: workflow.saved" in lines
    assert any(f'"workflow_id": "{wf}"' in line and '"version": 2' in line for line in lines)


def test_copy_puts_the_current_version_in_another_world_or_names_what_it_lacks(
    client: TestClient,
) -> None:
    a, b = _world(client), _world(client)
    wf = _workflow(client, a)
    client.post(f"/api/workflows/{wf}/versions", json={"yaml": WEEKLY.replace("Weekly", "Daily")})
    copied = client.post(f"/api/workflows/{wf}/copy", json={"world_id": b})
    assert copied.status_code == 201, copied.text
    assert copied.json()["number"] == 1 and copied.json()["model"]["name"] == "Daily short"
    assert copied.json()["workflow_id"] != wf
    assert [w["name"] for w in client.get(f"/api/worlds/{b}/workflows").json()] == ["Daily short"]
    assert len(client.get(f"/api/worlds/{a}/workflows").json()) == 1

    added = {"name": "render_video", "kind": "stage", "server": "x", "stage_types": ["connector"]}
    assert client.post(f"/api/worlds/{a}/connectors", json=added).status_code == 201
    uses = "name: Uses\nstages:\n  - connector: render\n    calls: render_video\n"
    special = _workflow(client, a, uses)
    refused = client.post(f"/api/workflows/{special}/copy", json={"world_id": b})
    assert refused.status_code == 422
    assert [v["rule"] for v in refused.json()["violations"]] == ["unknown_connector"]
    assert (
        client.post(f"/api/workflows/{wf}/copy", json={"world_id": "world_nope"}).status_code == 404
    )
    assert client.post("/api/workflows/wf_nope/copy", json={"world_id": b}).status_code == 404


def test_check_reports_the_next_run_of_a_schedule_and_refuses_a_bad_one(
    client: TestClient,
) -> None:
    world_id = _world(client)
    url = f"/api/worlds/{world_id}/workflows/check"
    assert client.post(url, json={"yaml": WEEKLY}).json()["next_run_at"] is None
    timed = client.post(url, json={"yaml": "schedule: '0 8 * * 1'\n" + WEEKLY}).json()
    assert timed["violations"] == [] and timed["next_run_at"]
    at = datetime.fromisoformat(timed["next_run_at"])
    assert at > datetime.now(UTC) and (at.hour, at.minute, at.weekday()) == (8, 0, 0)
    bad = client.post(url, json={"yaml": "schedule: every monday\n" + WEEKLY}).json()
    assert [v["rule"] for v in bad["violations"]] == ["schedule"] and bad["next_run_at"] is None
    refused = client.post(
        f"/api/worlds/{world_id}/workflows", json={"yaml": "schedule: x\n" + WEEKLY}
    )
    assert refused.status_code == 422


GATED = "name: Timed\nschedule: '* * * * * *'\nstages:\n  - gate: ok\n    ask: Fine?\n"


def test_a_saved_schedule_starts_runs_on_dbos_cron_until_the_next_version_drops_it(
    app: FastAPI, tmp_path: Path
) -> None:
    control = RunControl(run_storage(tmp_path), tmp_path / "dbos.sqlite", tmp_path, FakeNotifier())
    with TestClient(app) as running:
        world = _world(running)
        created = running.post(f"/api/worlds/{world}/workflows", json={"yaml": GATED})
        assert created.status_code == 201, created.text
        wf = created.json()["workflow_id"]

        # A per-second schedule can start several runs and the cap of 2 can leave any one of them
        # queued behind two parked at their gates, so wait for whichever reaches its gate.
        def gated() -> list[dict]:
            runs = running.get(f"/api/worlds/{world}/runs").json()
            return [r for r in runs if r["status"] == "waiting_gate"]

        until(lambda: len(gated()) >= 1)
        run = gated()[0]
        assert (run["workflow_id"], run["workflow_version"], run["name"]) == (wf, 1, "Timed")

        plain = running.post(
            f"/api/workflows/{wf}/versions",
            json={"yaml": GATED.replace("schedule: '* * * * * *'\n", "")},
        )
        assert plain.status_code == 201
        time.sleep(1.5)
        count = len(running.get(f"/api/worlds/{world}/runs").json())
        time.sleep(2.5)
        assert len(running.get(f"/api/worlds/{world}/runs").json()) == count

        # a world deleted with a scheduled workflow takes its schedule along
        other = _world(running)
        gone = _workflow(running, other, GATED)
        assert gone in control.schedules()
        assert running.delete(f"/api/worlds/{other}").status_code == 204
        assert gone not in control.schedules()
    # and on the next launch, DBOS's schedules follow the workflows table
    with TestClient(app) as again:
        world = _world(again)
        wf2 = _workflow(again, world, GATED)
        time.sleep(0.5)
    with TestClient(app) as third:
        until(
            lambda: any(
                r["workflow_id"] == wf2 for r in third.get(f"/api/worlds/{world}/runs").json()
            )
        )


def test_check_reports_what_would_stop_a_stage_only_after_spending(client: TestClient) -> None:
    world_id = _world(client)
    url = f"/api/worlds/{world_id}/workflows/check"
    kids = """name: Kids clickbait short
stages:
  - swarm: build video
    goal: Make the video.
    done_when: video.mp4 exists
    produces: [script.md, video.mp4]
  - connector: render
    calls: remotion_render
    input: script.md
    output: short.mp4
    params:
      project: /nowhere/at/all
  - gate: review
    ask: Approve?
  - connector: publish
    calls: youtube
    input: short.mp4
"""
    found = client.post(url, json={"yaml": kids}).json()["violations"]
    assert [(v["rule"], v["stage"], v["message"]) for v in found] == [
        (
            "swarm_binary",
            "build video",
            "only a connector stage can make video.mp4: swarm agents write text, so produce a"
            " script here and build the video from it in a remotion_build stage",
        ),
        ("connector_params", "render", "the Remotion project /nowhere/at/all does not exist"),
        (
            "connector_params",
            "render",
            "remotion_render needs params.composition, the composition ID",
        ),
        ("connector_params", "publish", "a publish stage needs params.title"),
    ]
    assert client.post(f"/api/worlds/{world_id}/workflows", json={"yaml": kids}).status_code == 422


def test_every_starter_workflow_passes_the_check_in_a_new_world(client: TestClient) -> None:
    world_id = _world(client)
    starters = client.get("/api/workflows/starters").json()
    names = [s["name"] for s in starters]
    assert names == ["Research brief", "Draft with approval", "Scheduled monitor", "Weekly short"]
    for starter in starters:
        check = {"yaml": starter["yaml"]}
        report = client.post(f"/api/worlds/{world_id}/workflows/check", json=check).json()
        assert report["violations"] == [], starter["name"]
        assert report["model"]["name"] == starter["name"] and starter["about"]
        assert (report["next_run_at"] is not None) == (starter["name"] == "Scheduled monitor")
    gated = [s["name"] for s in starters if "  - gate:" in s["yaml"]]
    assert gated == ["Draft with approval", "Weekly short"]


def test_every_eval_task_names_a_starter_and_runs_a_workflow_that_passes_the_check(
    client: TestClient,
) -> None:
    world_id = _world(client)
    starters = {s["name"] for s in client.get("/api/workflows/starters").json()}
    assert {t.workflow for t in TASKS} == starters
    for task in TASKS:
        check = {"yaml": workflow_yaml(task, 5.0)}
        report = client.post(f"/api/worlds/{world_id}/workflows/check", json=check).json()
        assert report["violations"] == [], task.id
