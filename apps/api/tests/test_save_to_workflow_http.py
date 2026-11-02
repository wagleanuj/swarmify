"""Also save to the workflow over HTTP (Run Assistant spec): an applied Run Change drafts the
workflow's next version by stage name, saves it when asked, names a newer version's difference and
needs it confirmed, and an Artifact Edit is drafted by the Run Assistant as a Stage Edit to the
stage that made the file. Real DBOS on SQLite, the scripted agent CLI behind the assistant."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_artifact_edit_http import FIVE, SEVEN, edit
from test_artifact_edit_http import at_gate as tagged
from test_run_changes_http import SHORT, at_gate, change, stage

from swarmify_connectors import FakeTransport
from swarmify_swarm import tool_id
from swarmify_swarm.fake import Answer, Call, Script


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def tagger(args: dict[str, Any]) -> dict[str, Any]:
    """`write` leaves metadata.md with seven tags."""
    if args.get("output_path"):
        Path(args["output_path"]).write_text(SEVEN)
    return {"ok": True}


def apply(client: TestClient, run: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    proposal = client.post(f"/api/runs/{run['id']}/changes/propose", json=body).json()
    applied = client.post(
        f"/api/runs/{run['id']}/changes", json={**body, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    return applied.json()


def save(client: TestClient, run: dict[str, Any], applied: dict[str, Any], **body: Any) -> Any:
    return client.post(f"/api/runs/{run['id']}/changes/{applied['id']}/save-to-workflow", json=body)


def test_a_stage_edit_is_drafted_then_saved_as_a_new_version_with_the_same_change(
    running: TestClient,
) -> None:
    run = at_gate(running)
    applied = apply(running, run, change(run, "render", params={"fps": 60}))

    drafted = save(running, run, applied)
    assert drafted.status_code == 200, drafted.text
    draft = drafted.json()
    assert (draft["stage"], draft["version"], draft["run_version"]) == ("render", 1, 1)
    assert draft["violations"] == [] and draft["conflicts"] == [] and draft["saved"] is None
    assert "fps: 30" in draft["before"] and "fps: 60" in draft["yaml"]
    versions = f"/api/workflows/{run['workflow_id']}/versions"
    assert len(running.get(versions).json()) == 1, "a draft saves nothing"

    saved = save(running, run, applied, save=True)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"]["number"] == 2
    [_, v2] = running.get(versions).json()
    assert v2["yaml"] == draft["yaml"]
    assert stage({"stages": v2["model"]["stages"]}, "render")["params"] == {"fps": 60}


def test_a_newer_workflow_version_is_named_and_saving_over_it_needs_confirming(
    running: TestClient,
) -> None:
    run = at_gate(running)
    applied = apply(running, run, change(run, "render", params={"fps": 60}))
    newer = SHORT.replace("output: short.mp4", "output: final.mp4")
    assert running.post(
        f"/api/workflows/{run['workflow_id']}/versions", json={"yaml": newer}
    ).is_success

    draft = save(running, run, applied).json()
    assert draft["conflicts"] == ["Since this run's v1, v2 changed render (output)."]
    refused = save(running, run, applied, save=True)
    assert refused.status_code == 409 and "v2 changed render" in refused.text

    saved = save(running, run, applied, save=True, confirmed=True).json()["saved"]
    render = stage({"stages": saved["model"]["stages"]}, "render")
    assert saved["number"] == 3 and (render["output"], render["params"]) == (
        "final.mp4",
        {"fps": 60},
    )


def test_a_skip_saves_without_the_stage(running: TestClient) -> None:
    run = at_gate(running)
    skipped = apply(running, run, {"kind": "skip", "stage": stage(run, "tag")["id"]})
    saved = save(running, run, skipped, save=True).json()["saved"]
    assert [s["name"] for s in saved["model"]["stages"]] == ["pick", "render", "render_video"]
    assert save(running, run, {**skipped, "id": "change_nope"}).status_code == 404


def test_an_artifact_edit_is_drafted_by_the_assistant_as_a_stage_edit_and_saved(
    running: TestClient, script: Script, transport: FakeTransport
) -> None:
    transport.outputs["tagger"] = [tagger]
    run = tagged(running)
    applied = apply(running, run, edit(run, "metadata.md", FIVE))
    script.scripts = [
        [
            Call(tool_id("read_run_file"), {"path": "nope.md"}),
            Call(tool_id("propose_change"), {"kind": "skip", "stage": "x"}),
            Answer(
                {
                    "reply": "Cap the tags at five where the file is written.",
                    "config": {"params": {"max_tags": 5}},
                }
            ),
        ]
    ]

    drafted = save(running, run, applied)
    assert drafted.status_code == 200, drafted.text
    draft = drafted.json()
    assert draft["stage"] == "write" and draft["config"] == {"params": {"max_tags": 5}}
    assert draft["reply"] == "Cap the tags at five where the file is written."
    assert "max_tags: 5" in draft["yaml"] and draft["violations"] == []
    assert "metadata.md" in script.prompts[0] and SEVEN in script.prompts[0]
    _, proposed = script.outcomes
    assert proposed.is_error, "drafting a save proposes nothing to the run"
    asked = running.get(f"/api/runs/{run['id']}/assistant").json()
    assert asked["messages"][-1]["text"] == draft["reply"] and asked["spent_usd"] > 0

    assert save(running, run, applied, save=True).status_code == 422
    saved = save(running, run, applied, save=True, config=draft["config"]).json()["saved"]
    write = saved["model"]["stages"][0]
    assert saved["number"] == 2 and write["params"] == {"max_tags": 5}


def test_a_rerun_has_nothing_to_save(running: TestClient, transport: FakeTransport) -> None:
    transport.outputs["tagger"] = [tagger]
    run = tagged(running)
    applied = apply(running, run, {"kind": "rerun", "stage": stage(run, "check")["id"]})
    refused = save(running, run, applied)
    assert refused.status_code == 409 and "Rerun" in refused.text


def test_saving_a_draft_the_workflow_moved_on_from_is_refused(running: TestClient) -> None:
    run = at_gate(running)
    applied = apply(running, run, change(run, "render", params={"fps": 60}))
    draft = save(running, run, applied).json()
    newer = SHORT.replace("output: short.mp4", "output: final.mp4")
    assert running.post(
        f"/api/workflows/{run['workflow_id']}/versions", json={"yaml": newer}
    ).is_success

    refused = save(running, run, applied, save=True, confirmed=True, version=draft["version"])
    assert refused.status_code == 409 and "moved on" in refused.text
    versions = running.get(f"/api/workflows/{run['workflow_id']}/versions").json()
    assert len(versions) == 2
