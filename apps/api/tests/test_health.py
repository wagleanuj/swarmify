from fastapi.testclient import TestClient

from swarmify_api.main import VERSION, app


def test_health_returns_version() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"version": VERSION}
    assert VERSION == "0.1.0"
