"""Basic auth on the LAN: `SWARMIFY_PASSWORD` turns it on for every page, API call and event
stream; only the health check stays open, for the container's healthcheck."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarmify_api import main


@pytest.fixture
def locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    monkeypatch.setenv("SWARMIFY_USER", "erlich")
    monkeypatch.setenv("SWARMIFY_PASSWORD", "aviato")
    return TestClient(main.create_app(tmp_path / "data"))


@pytest.mark.parametrize("path", ["/", "/worlds/world_abc", "/api/worlds", "/api/nope"])
def test_without_the_password_every_page_and_call_is_refused(locked: TestClient, path: str) -> None:
    for auth in (None, ("erlich", "wrong"), ("gilfoyle", "aviato")):
        refused = locked.get(path, auth=auth)
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == 'Basic realm="Swarmify"'


def test_the_password_opens_pages_and_calls(locked: TestClient) -> None:
    assert locked.get("/", auth=("erlich", "aviato")).text == "<div id=root></div>"
    assert locked.get("/api/worlds", auth=("erlich", "aviato")).json() == []
    assert locked.get("/api/nope", auth=("erlich", "aviato")).status_code == 404


def test_a_garbled_header_is_refused_not_an_error(locked: TestClient) -> None:
    for header in ("Basic !!!", "Basic ZXJsaWNo", "Bearer x", "Basic"):
        assert locked.get("/api/worlds", headers={"Authorization": header}).status_code == 401


def test_health_stays_open_for_the_container_healthcheck(locked: TestClient) -> None:
    assert locked.get("/api/health").json() == {"version": main.VERSION}


def test_no_password_means_no_auth_for_a_native_mac(client: TestClient) -> None:
    assert client.get("/api/worlds").status_code == 200
