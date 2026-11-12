from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarmify_api import main


def test_deep_links_fall_back_to_index_but_api_misses_stay_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("ok")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    client = TestClient(main.create_app(tmp_path / "data"))

    assert client.get("/").text == "<div id=root></div>"
    deep = client.get("/worlds/world_abc")
    assert deep.status_code == 200 and deep.text == "<div id=root></div>"
    assert client.get("/assets/app.js").text == "ok"
    assert client.get("/api/nope").status_code == 404


def test_the_installed_app_files_carry_their_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "manifest.webmanifest").write_text("{}")
    (dist / "sw.js").write_text("")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    client = TestClient(main.create_app(tmp_path / "data"))

    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"] == "application/manifest+json"
    sw = client.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache"
    assert sw.headers["service-worker-allowed"] == "/"
    assert "service-worker-allowed" not in client.get("/").headers
