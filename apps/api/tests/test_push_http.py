"""Web Push over HTTP (issue 459) with the push service mocked: subscribing a device, its
toggles, the test button, and real runs on DBOS pushing a gate and a failure with deep links."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_fakes import studio, until
from fastapi.testclient import TestClient

from swarmify_api import main
from swarmify_api.main import create_app
from swarmify_notifications.push import Target

QUICK = "name: Quick\nstages:\n  - gate: ok\n    ask: Go?\n    kind: confirm\n"
BOOM = "name: Boom\nstages:\n  - connector: explode\n    calls: studio\n"
P256DH = "B" + "A" * 86  # 65 bytes of base64url
AUTH = "A" * 22  # 16 bytes


class PushService:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.status = 201

    def __call__(self, target: Target, payload: bytes) -> int:
        self.sent.append((target.endpoint, json.loads(payload)))
        return self.status


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> PushService:
    fake = PushService()
    monkeypatch.setattr(main.push, "sender", lambda _keys: fake)
    return fake


@pytest.fixture
def running(tmp_path: Path, service: PushService) -> Iterator[TestClient]:
    def connector(_run: object, _id: str, stage: object) -> None:
        if getattr(stage, "name", "") == "explode":
            raise RuntimeError("connector said no")

    with TestClient(create_app(tmp_path, connector=connector, retro=False)) as client:
        yield client


def subscribe(client: TestClient, endpoint: str, label: str = "Pixel") -> dict[str, Any]:
    body = {"endpoint": endpoint, "keys": {"p256dh": P256DH, "auth": AUTH}, "label": label}
    res = client.post("/api/push/subscriptions", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_devices_subscribe_toggle_test_and_remove(
    running: TestClient, service: PushService, tmp_path: Path
) -> None:
    key = running.get("/api/push/key").json()["public_key"]
    assert key == json.loads((tmp_path / "vapid.json").read_text())["public"]
    assert "private" not in running.get("/api/push/key").text

    phone = subscribe(running, "https://push.example/phone")
    assert phone["gates"] and phone["notices"] and phone["last_success_at"] is None
    assert "endpoint" not in phone
    off = running.patch(f"/api/push/subscriptions/{phone['id']}", json={"notices": False})
    assert off.json()["notices"] is False and off.json()["gates"] is True

    assert running.post(f"/api/push/test/{phone['id']}").status_code == 204
    assert service.sent[-1][1]["body"] == "Notifications are on for this device."
    service.status = 500
    assert running.post(f"/api/push/test/{phone['id']}").status_code == 502
    service.status = 410
    assert running.post(f"/api/push/test/{phone['id']}").status_code == 404
    assert running.get("/api/push/subscriptions").json() == []

    again = subscribe(running, "https://push.example/phone")
    assert running.delete(f"/api/push/subscriptions/{again['id']}").status_code == 204
    assert running.delete(f"/api/push/subscriptions/{again['id']}").status_code == 404
    assert running.patch("/api/push/subscriptions/nope", json={}).status_code == 404
    bad = {"endpoint": "http://push.example/x", "keys": {"p256dh": P256DH, "auth": AUTH}}
    assert running.post("/api/push/subscriptions", json=bad).status_code == 422


def test_a_gate_and_a_failed_run_push_once_per_device_with_deep_links(
    running: TestClient, service: PushService
) -> None:
    subscribe(running, "https://push.example/phone")
    laptop = subscribe(running, "https://push.example/laptop", "Mac")
    running.patch(f"/api/push/subscriptions/{laptop['id']}", json={"failures": False})
    world_id = running.post("/api/worlds", json={"name": "Video"}).json()["id"]
    studio(running, world_id)

    def start(yaml: str) -> str:
        wf = running.post(f"/api/worlds/{world_id}/workflows", json={"yaml": yaml}).json()
        return running.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()["id"]

    run = start(QUICK)
    until(lambda: len(service.sent) == 2)
    gate = running.get("/api/gates").json()[0]
    for endpoint, msg in service.sent:
        assert msg == {
            "title": "Quick needs you",
            "body": "Go?",
            "url": f"/gates?gate={gate['stage_id']}",
            "tag": f"gate-{run}",
            "renotify": True,
        }, endpoint
    assert {e for e, _ in service.sent} == {
        "https://push.example/phone",
        "https://push.example/laptop",
    }
    running.post(f"/api/runs/{run}/decision", json={})
    until(lambda: running.get(f"/api/runs/{run}").json()["status"] == "done")

    failed = start(BOOM)
    until(lambda: len(service.sent) == 3)
    endpoint, msg = service.sent[2]
    assert endpoint == "https://push.example/phone", "the laptop has failures off"
    assert msg["title"] == "Boom failed" and msg["url"] == f"/worlds/{world_id}/runs/{failed}"
