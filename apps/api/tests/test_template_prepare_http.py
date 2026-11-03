"""A Template is prepared once when added (issue 352, ADR 0028): the fixture package `kit`
declares a prepare command, which the core runs in the sandbox from the Template's folder as a
background job, its status on the Template. Real sandbox, the fake ConnectorTransport."""

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from api_fakes import install, until
from fastapi import FastAPI
from fastapi.testclient import TestClient

# These tests run the fixture's prepare command for real, in the sandbox.
real_prepare = True

KIT = "packages/connectors/tests/fixtures/packages/kit"
PREPARE = """import pathlib, sys, time, urllib.request
here = pathlib.Path.cwd()
for outside in (here.parent / "escaped.txt", here.parents[1] / "escaped.txt"):
    try:
        outside.write_text("out")
    except OSError:
        pass
try:
    urllib.request.urlopen("http://example.com", timeout=5)
    (here / "reached").write_text("example.com")
except OSError:
    pass
if (here / "fail").exists():
    sys.exit("no lockfile here")
while not (here / "go").exists():
    time.sleep(0.05)
(here / "node_modules").mkdir()
(here / "node_modules/ok").write_text("installed")
"""


def zipped(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.fixture
def running(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def world_with_kit(client: TestClient) -> str:
    assert install(client, KIT).status_code == 201
    world = client.post("/api/worlds", json={"name": "Kits"}).json()["id"]
    enabled = client.put(f"/api/worlds/{world}/packages/kit", json={"version": "1.0.0"})
    assert enabled.status_code == 200, enabled.text
    return world


def status(client: TestClient, world: str, name: str) -> dict:
    listed = client.get(f"/api/worlds/{world}/templates").json()
    return next(t for t in listed if t["name"] == name)


def test_an_uploaded_template_is_prepared_once_in_its_folder_only(
    running: TestClient, tmp_path: Path
) -> None:
    client = running
    world = world_with_kit(client)
    url = f"/api/worlds/{world}/templates/kit_templates/cats"
    put = client.put(url, content=zipped({"prepare.py": PREPARE}))
    assert put.status_code == 201, put.text
    assert (put.json()["prepare"], put.json()["prepare_error"]) == ("preparing", "")
    assert status(client, world, "cats")["prepare"] == "preparing"
    folder = tmp_path / "worlds" / world / "assets/kit_templates/cats"
    (folder / "go").write_text("")
    until(lambda: status(client, world, "cats")["prepare"] != "preparing")
    cats = status(client, world, "cats")
    assert (cats["prepare"], cats["prepare_error"]) == ("ready", ""), cats
    assert (folder / "node_modules/ok").read_text() == "installed"
    assert cats["fingerprint"] == put.json()["fingerprint"]  # node_modules aside
    # it wrote its own folder only and reached no host outside prepare_network
    assert not (folder.parent / "escaped.txt").exists()
    assert not (folder.parents[1] / "escaped.txt").exists()
    assert not (folder / "reached").exists()


def test_a_failing_prepare_records_its_error_and_a_replace_prepares_again(
    running: TestClient,
) -> None:
    client = running
    world = world_with_kit(client)
    url = f"/api/worlds/{world}/templates/kit_templates/cats"
    failing = zipped({"prepare.py": PREPARE, "fail": ""})
    assert client.put(url, content=failing).status_code == 201
    until(lambda: status(client, world, "cats")["prepare"] != "preparing")
    cats = status(client, world, "cats")
    assert cats["prepare"] == "failed" and "no lockfile here" in cats["prepare_error"], cats
    assert "Kit's prepare command `python3 prepare.py` failed" in cats["prepare_error"]
    # still listed; replacing it prepares the new one
    fixed = client.put(f"{url}?replace=true", content=zipped({"prepare.py": PREPARE, "go": ""}))
    assert fixed.status_code == 201 and fixed.json()["prepare"] == "preparing"
    until(lambda: status(client, world, "cats")["prepare"] != "preparing")
    cats = status(client, world, "cats")
    assert (cats["prepare"], cats["prepare_error"]) == ("ready", ""), cats


def test_a_prepare_an_api_restart_cut_short_runs_again_on_startup(
    app: FastAPI, tmp_path: Path
) -> None:
    """A Template whose record still says preparing when the API starts (its job died with the
    last process) is prepared again, by the same background job as on add."""
    with TestClient(app) as client:
        world = world_with_kit(client)
    folder = tmp_path / "worlds" / world / "assets/kit_templates/cats"
    folder.mkdir(parents=True)
    (folder / "prepare.py").write_text(PREPARE)
    (folder / "go").write_text("")
    record = {"origin": "upload", "package": "kit", "added": "2026-11-01T10:00:00+00:00"}
    (folder / ".template.json").write_text(json.dumps(record | {"prepare": "preparing"}))
    with TestClient(app) as client:
        until(lambda: status(client, world, "cats")["prepare"] != "preparing")
        cats = status(client, world, "cats")
    assert (cats["prepare"], cats["prepare_error"]) == ("ready", ""), cats
    assert (folder / "node_modules/ok").read_text() == "installed"
