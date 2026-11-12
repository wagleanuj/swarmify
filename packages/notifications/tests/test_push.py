"""Web Push: the message for each run event, the subscriptions, a fake push service that
decrypts what the real sender posts, and dropping a subscription the browser has given up."""

import base64
import json
import os
import threading
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel import Event
from swarmify_kernel.sqlite import connect, migrate
from swarmify_notifications import MIGRATIONS, PushStorage
from swarmify_notifications.push import (
    Notice,
    Target,
    WebPush,
    generate_keys,
    load_keys,
    message,
    sender,
)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Browser:
    """A browser's side of a subscription: its key pair and auth secret, able to decrypt."""

    def __init__(self, endpoint: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = endpoint
        raw = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.p256dh = b64(raw)

    def read(self, body: bytes) -> dict[str, Any]:
        plain = http_ece.decrypt(
            body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"
        )
        return json.loads(plain)


def event(type: str, **payload: object) -> Event:
    return Event.new(
        type,
        world_id="world_1",
        run_id="run_1",
        payload={"run_id": "run_1", "name": "Weekly", **payload},
    )


GATE = {"stage_index": 1, "visit": 0, "stage": "pick", "kind": "pick", "ask": "Pick one idea."}


@pytest.fixture
def storage(tmp_path: Path) -> PushStorage:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    return PushStorage(db)


class FakeSend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.status = 201

    def __call__(self, target: Target, payload: bytes) -> int:
        self.sent.append((target.endpoint, json.loads(payload)))
        return self.status


def test_each_run_event_has_generic_copy_and_a_deep_link() -> None:
    gate = message(event("gate.opened", **GATE), "stage_9")
    assert gate is not None
    assert (gate.kind, gate.title, gate.body) == ("gates", "Weekly needs you", "Pick one idea.")
    assert (gate.url, gate.tag, gate.renotify) == ("/gates?gate=stage_9", "gate-run_1", True)
    assert message(event("gate.opened", **GATE), None) is None, "answered before it was sent"

    budget = message(event("gate.opened", **{**GATE, "kind": "budget", "ask": ""}), "s")
    assert budget is not None and budget.kind == "budget" and "budget is spent" in budget.body
    warn = message(event("budget.warning", spent_usd=8, budget_usd=10), None)
    assert warn is not None and warn.body == "Spent $8.00 of $10.00. It pauses at 100 percent."
    failed = message(event("run.ended", status="failed"), None)
    assert failed is not None and failed.kind == "failures"
    assert failed.url == "/worlds/world_1/runs/run_1"
    assert message(event("run.ended", status="done"), None) is None
    assert message(event("run.updated"), None) is None


def test_one_push_per_gate_to_each_device_with_that_kind_on(storage: PushStorage) -> None:
    send = FakeSend()
    push = WebPush(storage, send, lambda run, index: f"stage_{index}")
    a = Browser("https://push.example/a")
    b = Browser("https://push.example/b")
    phone = push.subscribe(a.endpoint, a.p256dh, b64(a.auth), "Phone")
    laptop = push.subscribe(b.endpoint, b.p256dh, b64(b.auth), "")
    assert laptop["label"] == "A browser"
    assert push.toggle(laptop["id"], {"gates": False, "nonsense": True})

    opened = event("gate.opened", **GATE)
    push.notify(opened)
    push.notify(opened)  # a replayed save: still one push
    push.notify(event("run.ended", status="failed"))
    push.flush()
    assert [(e, m["tag"]) for e, m in send.sent] == [
        (a.endpoint, "gate-run_1"),
        (a.endpoint, "run-run_1"),
        (b.endpoint, "run-run_1"),
    ]
    assert "kind" not in send.sent[0][1] and send.sent[0][1]["url"] == "/gates?gate=stage_1"
    devices = {d["id"]: d for d in push.devices()}
    assert devices[phone["id"]]["last_success_at"] and devices[laptop["id"]]["gates"] is False
    assert "endpoint" not in devices[phone["id"]] and "p256dh" not in devices[phone["id"]]

    # The same endpoint again refreshes its keys and keeps its toggles.
    again = push.subscribe(b.endpoint, a.p256dh, b64(a.auth), "Laptop")
    assert again["id"] == laptop["id"] and again["gates"] is False and again["label"] == "Laptop"
    assert len(push.devices()) == 2


def test_a_gone_subscription_is_deleted_and_bad_ones_are_refused(storage: PushStorage) -> None:
    send = FakeSend()
    push = WebPush(storage, send)
    a = Browser("https://push.example/a")
    device = push.subscribe(a.endpoint, a.p256dh, b64(a.auth), "Phone")
    send.status = 500
    assert push.test(device["id"]) == 500 and len(push.devices()) == 1
    send.status = 410
    assert push.test(device["id"]) == 410
    assert push.devices() == []
    with pytest.raises(KeyError):
        push.test(device["id"])
    with pytest.raises(ValueError, match="https"):
        push.subscribe("http://push.example/a", a.p256dh, b64(a.auth), "")
    with pytest.raises(ValueError, match="keys"):
        push.subscribe(a.endpoint, "short", b64(a.auth), "")
    assert push.unsubscribe("nope") is False


def test_new_notices_are_pushed_once_and_the_first_list_is_the_baseline(
    storage: PushStorage,
) -> None:
    send = FakeSend()
    push = WebPush(storage, send)
    a = Browser("https://push.example/a")
    push.subscribe(a.endpoint, a.p256dh, b64(a.auth), "Phone")
    standing = Notice("sign_in", "Studio needs sign-in.", "world_1")
    push.notices([standing])
    push.flush()
    assert send.sent == []

    waiting = Notice("network", "A run is waiting for the network, for 31 min now.", "w", "run_2")
    push.notices([standing, waiting])
    later = Notice("network", "A run is waiting for the network, for 32 min now.", "w", "run_2")
    push.notices([standing, later])  # the same notice, its minutes ticked
    push.flush()
    assert [m["title"] for _, m in send.sent] == ["Needs you"]
    assert send.sent[0][1]["url"] == "/worlds/w/runs/run_2"


@pytest.fixture
def push_service() -> Iterator[tuple[str, list[tuple[dict[str, str], bytes]]]]:
    got: list[tuple[dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append(({k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(201 if self.path == "/ok" else 410)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()
    server.server_close()


def test_the_real_sender_encrypts_for_the_browser_and_signs_with_vapid(
    tmp_path: Path, push_service: tuple[str, list[tuple[dict[str, str], bytes]]]
) -> None:
    url, got = push_service
    keys = load_keys(tmp_path, {}, "https://box.example.ts.net/")
    assert keys.subject == "https://box.example.ts.net"
    assert (tmp_path / "vapid.json").stat().st_mode & 0o777 == 0o600
    assert load_keys(tmp_path, {}, "").public == keys.public, "made once, then kept"
    public, private = generate_keys()
    env = {"SWARMIFY_VAPID_PUBLIC_KEY": public, "SWARMIFY_VAPID_PRIVATE_KEY": private}
    assert load_keys(tmp_path, env, "http://localhost:8000").public == public

    browser = Browser(f"{url}/ok")
    send = sender(keys)
    status = send(Target(browser.endpoint, browser.p256dh, b64(browser.auth)), b'{"title":"Hi"}')
    assert status == 201
    headers, body = got[0]
    assert headers["content-encoding"] == "aes128gcm" and headers["ttl"]
    assert (
        headers["authorization"].startswith("vapid t=")
        and f"k={keys.public}" in headers["authorization"]
    )
    assert browser.read(body) == {"title": "Hi"}
    assert send(Target(f"{url}/gone", browser.p256dh, b64(browser.auth)), b"{}") == 410
