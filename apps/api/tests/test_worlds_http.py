from pathlib import Path

from fastapi.testclient import TestClient


def test_create_list_get_delete_world(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/api/worlds", json={"name": " Video ", "purpose": "Weekly shorts."})
    assert created.status_code == 201
    world = created.json()
    assert world["name"] == "Video" and world["purpose"] == "Weekly shorts."
    assert world["id"].startswith("world_") and world["created_at"]
    assert (tmp_path / "worlds" / world["id"]).is_dir()

    assert client.get("/api/worlds").json() == [world]
    assert client.get(f"/api/worlds/{world['id']}").json() == world

    assert client.delete(f"/api/worlds/{world['id']}").status_code == 204
    assert not (tmp_path / "worlds" / world["id"]).exists()
    assert client.get("/api/worlds").json() == []
    assert client.get(f"/api/worlds/{world['id']}").status_code == 404
    assert client.delete(f"/api/worlds/{world['id']}").status_code == 404


def test_create_world_rejects_blank_or_too_long_names(client: TestClient) -> None:
    assert client.post("/api/worlds", json={"name": "   "}).status_code == 422
    assert client.post("/api/worlds", json={"name": "x" * 121}).status_code == 422
    assert client.post("/api/worlds", json={}).status_code == 422
    assert client.get("/api/worlds").json() == []


def test_purpose_is_optional(client: TestClient) -> None:
    assert client.post("/api/worlds", json={"name": "Video"}).json()["purpose"] == ""
