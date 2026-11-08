"""Artifact Edit over HTTP (ADR 0027): a text file a finished stage made is proposed with the
stages that already read it, applied with its earlier content kept and served, streamed as the
artifact event the Library and Timeline reload on, and read by the stages still to come. A typed
artifact is checked against the type its connector declares; a binary is refused. Real DBOS on
SQLite, the fake ConnectorTransport behind the stage calls."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_fakes import until
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sse import sse_events
from swarmify_connectors import FakeTransport

TAGS = {
    "type": "object",
    "properties": {"tags": {"type": "array", "maxItems": 5, "items": {"type": "string"}}},
    "required": ["tags"],
}
TAGGER = {
    "name": "tagger",
    "kind": "stage",
    "server": "uvx tagger-mcp",
    "stage_types": ["connector"],
    "types": {"metadata": TAGS},
}
FLOW = """name: Tag then upload
stages:
  - connector: write
    calls: tagger
    output: metadata.md
  - connector: check
    calls: tagger
    input: metadata.md
  - gate: confirm
    ask: Upload it?
    kind: approve
  - connector: upload
    calls: tagger
    input: metadata.md
"""


def metadata(*tags: str) -> str:
    fields = ", ".join(f'"{t}"' for t in tags)
    return f'---\ntype: "metadata"\n---\n\n```json\n{{"tags": [{fields}]}}\n```\n'


SEVEN = metadata("a", "b", "c", "d", "e", "f", "g")
FIVE = metadata("a", "b", "c", "d", "e")


@pytest.fixture
def read(transport: FakeTransport) -> dict[str, str]:
    """The text each stage was given as its `input`, by the stage's id. `write` leaves the
    metadata with seven tags and a video beside it."""
    seen: dict[str, str] = {}

    def tagger(args: dict[str, Any]) -> dict[str, Any]:
        if args.get("output_path"):
            out = Path(args["output_path"])
            out.write_text(SEVEN)
            out.with_name("short.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42\xff\xfe")
        else:
            seen[args["idempotency_key"]] = Path(args["input_path"]).read_text()
        return {"ok": True}

    transport.outputs["tagger"] = [tagger]
    return seen


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def at_gate(client: httpx.Client | TestClient) -> dict[str, Any]:
    world = client.post("/api/worlds", json={"name": "Shorts"}).json()["id"]
    assert client.post(f"/api/worlds/{world}/connectors", json=TAGGER).status_code == 201
    workflow = client.post(f"/api/worlds/{world}/workflows", json={"yaml": FLOW}).json()
    run = client.post(f"/api/workflows/{workflow['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
    return client.get(f"/api/runs/{run['id']}").json()


def edit(run: dict[str, Any], path: str, text: str) -> dict[str, Any]:
    payload = {"path": path, "text": text}
    return {"kind": "artifact_edit", "stage": run["stages"][0]["id"], "payload": payload}


def test_an_artifact_edit_is_proposed_applied_kept_streamed_and_read_by_the_next_stage(
    live_url: str, read: dict[str, str]
) -> None:
    client = httpx.Client(base_url=live_url, timeout=10)
    run = at_gate(client)
    change = edit(run, "metadata.md", FIVE)

    proposal = client.post(f"/api/runs/{run['id']}/changes/propose", json=change).json()
    assert proposal["violations"] == [], proposal
    assert (proposal["before"]["text"], proposal["after"]["text"]) == (SEVEN, FIVE)
    assert proposal["read_by"] == ["check"]

    applied = client.post(
        f"/api/runs/{run['id']}/changes", json={**change, "revision": proposal["revision"]}
    )
    assert applied.status_code == 201, applied.text
    kept = applied.json()["before"]["kept"]
    earlier = client.get(f"/api/worlds/{run['world_id']}/files/{kept}")
    assert earlier.status_code == 200 and earlier.text == SEVEN
    listed = client.get(f"/api/runs/{run['id']}/changes").json()
    assert [(c["kind"], c["stage_name"]) for c in listed] == [("artifact_edit", "write")]

    with httpx.stream(
        "GET", f"{live_url}/api/worlds/{run['world_id']}/events?since=0", timeout=10
    ) as r:
        created = next(
            data
            for kind, _, data in sse_events(r)
            if kind == "artifact.created" and data["payload"]["author"] == "operator"
        )
    assert created["payload"]["path"].endswith(f"{run['stages'][0]['id']}/artifacts/metadata.md")
    assert created["payload"]["type"] == "metadata"

    assert client.post(f"/api/runs/{run['id']}/decision", json={}).is_success
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    assert read == {run["stages"][1]["id"]: SEVEN, run["stages"][3]["id"]: FIVE}


def test_a_typed_artifact_that_no_longer_fits_its_type_or_a_binary_is_refused(
    running: TestClient, read: dict[str, str]
) -> None:
    run = at_gate(running)
    change = edit(run, "metadata.md", metadata("a", "b", "c", "d", "e", "f", "g", "h"))

    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=change).json()
    assert proposal["violations"] == [
        "metadata.md no longer fits type metadata at tags: ['a', 'b', 'c', 'd', 'e', 'f', 'g',"
        " 'h'] is too long"
    ]
    applied = running.post(
        f"/api/runs/{run['id']}/changes", json={**change, "revision": proposal["revision"]}
    )
    assert applied.status_code == 422 and "no longer fits type metadata" in applied.text
    untyped = edit(run, "metadata.md", '---\ntype: "metadata"\n---\n\nNo fields.\n')
    proposal = running.post(f"/api/runs/{run['id']}/changes/propose", json=untyped).json()
    assert proposal["violations"] == [
        "metadata.md no longer fits type metadata: its fields need a JSON block"
    ]

    for stripped in ("No fields.\n", "---\ntype: 'notes'\n---\n\nNo fields.\n"):
        proposal = running.post(
            f"/api/runs/{run['id']}/changes/propose", json=edit(run, "metadata.md", stripped)
        ).json()
        assert proposal["violations"] == ["metadata.md must keep type metadata"]

    video = running.post(
        f"/api/runs/{run['id']}/changes/propose", json=edit(run, "short.mp4", "not a video")
    )
    assert video.status_code == 422 and "short.mp4 is not a text file" in video.text
    assert running.get(f"/api/runs/{run['id']}/changes").json() == []
