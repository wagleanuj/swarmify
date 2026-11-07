"""The Build Session Capability is generic (ADR 0018, issue 217): the fixture package `sketch`
requests one in its Manifest, and a run through the API gives it a sandboxed session with its
system prompt, brief, starter and network, charged to the run's budget, then calls its own
`finish` tool on the result as a long job. Real DBOS on SQLite, the fake ConnectorTransport and
the Agent SDK client with a scripted CLI."""

import json
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from api_fakes import install
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_connectors import Connector, ConnectorError, FakeTransport
from swarmify_swarm import tool_id
from swarmify_swarm.fake import Answer, Call, Say, Script

SKETCH = "packages/connectors/tests/fixtures/packages/sketch"
FLOW = """name: A page
stages:
  - connector: copy
    calls: draft
    output: copy.md
  - connector: colours
    calls: draft
    output: palette.json
  - connector: page
    calls: sketch
    inputs: [copy.md, palette.json]
    output: page.html
    params: {tone: loud, brief: Big letters.}
"""
DRAFT = {"name": "draft", "kind": "stage", "server": "uvx draft-mcp", "stage_types": ["connector"]}


class Writing(FakeTransport):
    """Writes each stage's output, so the stage after it finds its input."""

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        if arguments.get("output_path") and tool != "finish":
            Path(arguments["output_path"]).write_text("written")
        return super().call(connector, tool, arguments, env)


@pytest.fixture
def transport() -> FakeTransport:
    def done(args: dict[str, Any]) -> dict[str, Any]:
        Path(args["job_id"]).write_text("<html>published</html>")
        return {"status": "done"}

    return Writing(
        outputs={
            "finish": [lambda a: {"job_id": a["output_path"], "poll": "finish_status"}],
            "finish_status": [{"status": "running", "progress": 0.5}, done],
        }
    )


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_package_requesting_a_build_session_gets_one_then_its_tool_runs_on_the_result(
    running: TestClient, transport: FakeTransport, script: Script, tmp_path: Path
) -> None:
    client = running
    script.scripts = [[Answer(lambda o: (Path(o.cwd) / "page.css").write_text("x")), Say("Done.")]]
    script.cost = 0.25
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    enabled = client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"})
    assert enabled.status_code == 200, enabled.text
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": FLOW})
    assert saved.status_code == 201, saved.text
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "done", run

    copy_id, palette_id, page_id = (s["id"] for s in run["stages"])
    folder = tmp_path / "worlds" / w / "runs" / run["id"]
    build = folder / page_id / "build"
    copy, palette = (
        folder / copy_id / "artifacts/copy.md",
        folder / palette_id / "artifacts/palette.json",
    )

    # the session: the package's system prompt, brief and note, seeded from its starter, its
    # network only, sandboxed, and paid from the run's budget
    (options,) = script.sessions
    assert options.cwd == build
    assert options.system_prompt == "You are the page writer of the sketch fixture.\n"
    assert (options.sandbox or {}).get("network") == {"allowedDomains": ["pypi.org"]}
    assert (options.sandbox or {}).get("allowUnsandboxedCommands") is False
    prompt = script.prompts[0]
    assert prompt.startswith(f"Write a loud page for the copy at {copy}.")
    assert f"Use the colours in {palette} (palette.json)." in prompt
    assert f"The stage's other inputs, also yours to read: {palette}." in prompt
    assert "The operator's brief: Big letters." in prompt
    assert (build / "index.html").is_file() and (build / "page.css").is_file()
    assert run["spent_usd"] == pytest.approx(0.25)

    # then its own tool on the result, a long job polled to the end, its output the artifact
    finish = [c for c in transport.calls if c[1] == "finish"]
    assert [(c[0], c[2]["site"], c[2]["tone"]) for c in finish] == [("sketch", str(build), "loud")]
    assert finish[0][2]["input_path"] == str(copy)
    assert [c[1] for c in transport.calls if c[0] == "sketch"] == ["finish"] + ["finish_status"] * 2
    call = client.get(f"/api/stages/{page_id}/call").json()
    assert (call["connector"], call["tool"], call["status"]) == ("sketch", "finish", "done")
    [artifact] = client.get(f"/api/runs/{run['id']}/stages/{page_id}/artifacts").json()
    assert artifact["name"] == "page.html"


def test_a_build_session_gets_its_packages_tool_servers_confined_to_the_stage(
    running: TestClient, script: Script, tmp_path: Path
) -> None:
    """Issue 403: the session is offered the server of the package it renders with, allowed only
    the tools its Manifest lends (`[build_session].tools`, never the render), and each package
    `params.tools` names, whole; each server reads the run and writes only the stage."""
    client = running
    script.scripts = [[Say("Done.")]]
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    assert client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"}).is_success
    flow = FLOW.replace("brief: Big letters.}", "tools: [draft]}")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": flow})
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "done", run

    (options,) = script.sessions
    assert {"sketch", "draft"} <= set(options.mcp_servers)  # type: ignore[arg-type]
    assert {"mcp__sketch__preview", "mcp__draft__*"} <= set(options.allowed_tools)
    assert "mcp__sketch__*" not in options.allowed_tools and "mcp__sketch__finish" not in str(
        options.allowed_tools
    )
    runs = (tmp_path / "worlds" / w / "runs" / run["id"]).resolve()
    stage = runs / run["stages"][2]["id"]
    for name in ("sketch", "draft"):
        server: dict[str, Any] = options.mcp_servers[name]  # type: ignore[index,assignment]
        files = json.loads(Path(server["args"][1]).read_text())["sandbox"]["filesystem"]
        assert str(runs / run["stages"][0]["id"]) in files["allowRead"]  # the run around it
        worlds = (tmp_path / "worlds").resolve()  # besides its own scratch HOME
        assert [p for p in files["allowWrite"] if Path(p).is_relative_to(worlds)] == [str(stage)]


def test_a_build_whose_tool_package_is_not_enabled_fails_before_its_session(
    running: TestClient, script: Script
) -> None:
    client = running
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    assert client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"}).is_success
    flow = FLOW.replace("brief: Big letters.}", "tools: [nope]}")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": flow})
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "failed"
    assert "sketch needs nope, not enabled for this world" in run["stages"][2]["error"]
    assert script.sessions == []


@pytest.mark.parametrize("guided", [True, False])
def test_a_build_with_a_base_starts_from_a_copy_of_the_worlds_template(
    running: TestClient, transport: FakeTransport, script: Script, tmp_path: Path, guided: bool
) -> None:
    """Issue 348: `base` names a Template in the world's Templates folder of the package (ADR
    0028). The build starts from a copy, the Template stays as it was, the call records its name
    and fingerprint, and the brief names its guide file when it has one."""
    client = running
    script.scripts = [[Answer(lambda o: (Path(o.cwd) / "page.css").write_text("x")), Say("Done.")]]
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    enabled = client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"})
    assert enabled.status_code == 200, enabled.text
    bold = tmp_path / "worlds" / w / "assets/sketches/bold"
    (bold / "node_modules/x").mkdir(parents=True)
    (bold / "index.html").write_text("<h1>bold</h1>")
    (bold / ".template.json").write_text('{"origin": "upload"}')
    if guided:
        (bold / "GUIDE.md").write_text("Shout.")
    before = sorted((p.relative_to(bold), p.read_bytes()) for p in bold.rglob("*") if p.is_file())
    flow = FLOW.replace("{tone: loud,", "{base: bold, tone: loud,")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": flow})
    assert saved.status_code == 201, saved.text
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "done", run

    page_id = run["stages"][2]["id"]
    build = tmp_path / "worlds" / w / "runs" / run["id"] / page_id / "build"
    assert (build / "index.html").read_text() == "<h1>bold</h1>"
    assert (build / "page.css").is_file() and not (build / "node_modules").exists()
    after = sorted((p.relative_to(bold), p.read_bytes()) for p in bold.rglob("*") if p.is_file())
    assert after == before  # the builder wrote its copy, never the Template

    prompt = script.prompts[0]
    guide = f"Read {build / 'GUIDE.md'} first: the guide of the Template bold this build starts"
    assert (guide in prompt) is guided
    assert prompt.startswith("Write a loud page for the copy at ")
    call = client.get(f"/api/stages/{page_id}/call").json()
    assert call["template"]["name"] == "bold"
    fingerprint = call["template"]["fingerprint"]
    assert len(fingerprint) == 64

    # the same files make the same fingerprint, whatever node_modules or the record hold
    (bold / ".template.json").write_text('{"origin": "run"}')
    (bold / "node_modules/y").mkdir()
    script.scripts = [[Say("Done.")]]
    again = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{again['id']}").json()["status"] in ("done", "failed"))
    second = client.get(f"/api/runs/{again['id']}").json()["stages"][2]["id"]
    assert client.get(f"/api/stages/{second}/call").json()["template"]["fingerprint"] == fingerprint


@pytest.mark.parametrize("added_by", ["sketch_plain", "sketch", None])
def test_a_shared_templates_folder_applies_the_rules_of_the_package_that_added_each_template(
    running: TestClient, script: Script, tmp_path: Path, added_by: str | None
) -> None:
    """A build package and another declaring the same Templates folder share it (ADR 0028):
    `sketch_plain`, a copy of sketch without a guide, claims `sketches` too. A Template's rules
    (here its guide) come from the package its record says added it; one whose record names
    none, as one placed by hand, gets the folder's first claimant's, sketch's."""
    client = running
    script.scripts = [[Say("Done.")]]
    plain = tmp_path / "src/sketch_plain"
    shutil.copytree(Path(__file__).parents[3] / SKETCH, plain)
    toml = plain / "swarmify-connector.toml"
    text = toml.read_text().replace('name = "sketch"', 'name = "sketch_plain"', 1)
    toml.write_text(text.replace('guide = "GUIDE.md"\n', ""))
    for source in (SKETCH, str(plain)):
        assert install(client, source).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    for name in ("sketch", "sketch_plain"):
        enabled = client.put(f"/api/worlds/{w}/packages/{name}", json={"version": "1.0.0"})
        assert enabled.status_code == 200, enabled.text
    bold = tmp_path / "worlds" / w / "assets/sketches/bold"
    bold.mkdir(parents=True)
    (bold / "index.html").write_text("<h1>bold</h1>")
    (bold / "GUIDE.md").write_text("Shout.")
    record = {"origin": "upload"} | ({"package": added_by} if added_by else {})
    (bold / ".template.json").write_text(json.dumps(record))
    flow = FLOW.replace("{tone: loud,", "{base: bold, tone: loud,")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": flow})
    assert saved.status_code == 201, saved.text
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "done"
    guided = "GUIDE.md first: the guide of the Template bold" in script.prompts[0]
    assert guided is (added_by != "sketch_plain")


def test_the_workflow_check_flags_a_base_the_world_does_not_have(
    running: TestClient, tmp_path: Path
) -> None:
    """Issue 348: the base resolves in the world's own Templates, never another world's or the
    data directory's."""
    client = running
    assert install(client, SKETCH).status_code == 201
    w, other = (client.post("/api/worlds", json={"name": n}).json()["id"] for n in ("A", "B"))
    for world in (w, other):
        enabled = client.put(f"/api/worlds/{world}/packages/sketch", json={"version": "1.0.0"})
        assert enabled.status_code == 200, enabled.text
    (tmp_path / "worlds" / other / "assets/sketches/bold").mkdir(parents=True)
    (tmp_path / "sketches/bold").mkdir(parents=True)
    yaml = "name: P\nstages:\n  - connector: page\n    calls: sketch\n    input: copy.md\n"

    def problems(world: str, base: str) -> list[str]:
        """Its violations about the base; its input from no earlier stage is beside the point."""
        flow = f"{yaml}    params: {{base: {base}}}\n"
        checked = client.post(f"/api/worlds/{world}/workflows/check", json={"yaml": flow})
        assert checked.status_code == 200, checked.text
        return [v["message"] for v in checked.json()["violations"] if "Template" in v["message"]]

    assert any("the Template bold is not one of this world's" in p for p in problems(w, "bold"))
    assert any("a Template's name, not a path" in p for p in problems(w, "../bold"))
    assert problems(other, "bold") == []


def test_a_finished_build_is_saved_as_a_template_without_what_belonged_to_its_run(
    running: TestClient, transport: FakeTransport, script: Script, tmp_path: Path
) -> None:
    """Issue 350: the sketch fixture declares Templates. Saving its finished build copies the
    build folder, never its revisions, without the package's `strip` paths and node_modules,
    checks `require`, and records the run and stage it came from."""
    client = running

    def builds(o: Any) -> None:
        build = Path(o.cwd)
        for name in ("page.css", "page.html", "scratch/notes.md", "node_modules/x/index.js"):
            (build / name).parent.mkdir(parents=True, exist_ok=True)
            (build / name).write_text("x")

    script.scripts = [[Answer(builds), Say("Done.")]]
    # the first run's render fails, so its build never finished
    transport.outputs["finish"].insert(0, ConnectorError("the render broke"))
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    enabled = client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"})
    assert enabled.status_code == 200
    flow = client.post(f"/api/worlds/{w}/workflows", json={"yaml": FLOW}).json()["workflow_id"]

    def ran() -> dict[str, Any]:
        run = client.post(f"/api/workflows/{flow}/runs", json={}).json()
        until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
        return client.get(f"/api/runs/{run['id']}").json()

    failed = ran()
    assert failed["status"] == "failed", failed
    page = failed["stages"][2]["id"]
    url = f"/api/runs/{failed['id']}/stages/{page}/template"
    unfinished = client.post(url, json={"name": "loud"})
    assert unfinished.status_code == 422 and "finished" in unfinished.json()["detail"]

    script.scripts = [[Answer(builds), Say("Done.")]]
    run = ran()
    assert run["status"] == "done", run
    copy, _, page = (s["id"] for s in run["stages"])
    build = tmp_path / "worlds" / w / "runs" / run["id"] / page / "build"
    (build.with_name("build.v1")).mkdir()  # a revision beside it is never part of it
    url = f"/api/runs/{run['id']}/stages/{page}/template"
    saved = client.post(url, json={"name": "loud"})
    assert saved.status_code == 201, saved.json()
    t = saved.json()
    assert (t["name"], t["folder"], t["origin"], t["run_id"], t["stage"]) == (
        "loud", "sketches", "run", run["id"], "page",
    )  # fmt: skip
    folder = tmp_path / "worlds" / w / "assets/sketches"
    files = sorted(p.relative_to(folder / "loud").as_posix() for p in folder.rglob("*.*"))
    assert files == [".template.json", "index.html", "page.css"]
    assert len(t["fingerprint"]) == 64 and t["bytes"] == len("<html></html>\n") + 1
    assert client.get(f"/api/worlds/{w}/templates").json() == [t]
    assert json.loads((folder / "loud/.template.json").read_text())["package"] == "sketch"
    assert (build / "page.html").is_file()  # the run's own build is untouched

    # an existing name and a missing required file behave as on upload
    taken = client.post(url, json={"name": "loud"})
    assert taken.status_code == 409 and "loud is already" in taken.json()["detail"]
    assert client.post(url, json={"name": "loud", "replace": True}).status_code == 201
    assert client.post(url, json={"name": "a b"}).status_code == 422
    (build / "index.html").unlink()
    lacking = client.post(url, json={"name": "bare"})
    assert lacking.status_code == 422 and "lacks index.html" in lacking.json()["detail"]
    assert sorted(p.name for p in folder.iterdir()) == ["loud"]  # no leftovers

    # a stage whose package declares no Templates, and stages that are not there
    plain = client.post(f"/api/runs/{run['id']}/stages/{copy}/template", json={"name": "x"})
    assert plain.status_code == 422 and "Templates" in plain.json()["detail"]
    for missing in (f"/api/runs/{run['id']}/stages/nope", "/api/runs/nope/stages/nope"):
        assert client.post(f"{missing}/template", json={"name": "x"}).status_code == 404


def test_the_run_assistant_reads_which_template_a_build_started_from(
    running: TestClient, transport: FakeTransport, script: Script, tmp_path: Path
) -> None:
    """Issue 354: get_run shows a build stage's Template, its name and fingerprint as the call
    recorded them, so the assistant answers from the run's record."""
    client = running
    assert install(client, SKETCH).status_code == 201
    w = client.post("/api/worlds", json={"name": "Pages"}).json()["id"]
    assert client.post(f"/api/worlds/{w}/connectors", json=DRAFT).status_code == 201
    enabled = client.put(f"/api/worlds/{w}/packages/sketch", json={"version": "1.0.0"})
    assert enabled.status_code == 200, enabled.text
    bold = tmp_path / "worlds" / w / "assets/sketches/bold"
    bold.mkdir(parents=True)
    (bold / "index.html").write_text("<h1>bold</h1>")
    flow = FLOW.replace("{tone: loud,", "{base: bold, tone: loud,")
    saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": flow})
    script.scripts = [[Say("Done.")]]
    run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
    until(lambda: client.get(f"/api/runs/{run['id']}").json()["status"] in ("done", "failed"))
    page_id = client.get(f"/api/runs/{run['id']}").json()["stages"][2]["id"]
    recorded = client.get(f"/api/stages/{page_id}/call").json()["template"]

    said = f"It started from the Template bold, fingerprint {recorded['fingerprint']}."
    sources = [{"label": "the page stage's Template", "stage": page_id}]
    script.scripts = [[Call(tool_id("get_run"), {}), Answer({"reply": said, "sources": sources})]]
    asked = client.post(f"/api/runs/{run['id']}/assistant", json={"message": "From what?"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["sources"] == sources
    [got] = script.outcomes
    stages = json.loads(got.text)["stages"]
    assert stages[2]["template"] == recorded and recorded["name"] == "bold"
    assert stages[0]["template"] is None  # a stage that started from no Template
