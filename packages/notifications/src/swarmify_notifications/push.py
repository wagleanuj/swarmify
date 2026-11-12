"""Web Push: the second adapter of the Runs Notifier port (issue 459), beside Slack. Each browser
the operator turned notifications on in is a subscription; a gate, a failed run, a spent budget
and a Needs-you notice reach every subscription with that kind switched on.

Payloads are encrypted (RFC 8291, aes128gcm) and signed with the server's VAPID key (RFC 8292),
then POSTed to the browser's push service. Sends run on one background thread, so a run never
waits on a push service. Nothing here logs an endpoint, a key or a payload.
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Queue
from urllib.parse import urlparse

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from swarmify_kernel import Event
from swarmify_notifications.sqlite import PushStorage

log = logging.getLogger(__name__)

KINDS = ("gates", "failures", "budget", "notices")
TIMEOUT = 10.0
TTL = 24 * 3600
"""A push the phone was off for still arrives within a day; after that it is stale."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Keys:
    """The VAPID key pair: `public` (the browser's applicationServerKey) and `private`, both
    base64url of the raw P-256 key. `subject` is the mailto: or https: contact push services ask
    for."""

    public: str
    private: str
    subject: str


def generate_keys() -> tuple[str, str]:
    """A new VAPID pair as (public, private)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(public), private


def load_keys(data_dir: Path, env: dict[str, str], base_url: str) -> Keys:
    """SWARMIFY_VAPID_PUBLIC_KEY and SWARMIFY_VAPID_PRIVATE_KEY when both are set, else the pair
    in `<data_dir>/vapid.json` (mode 600), made on first use. The subject is
    SWARMIFY_VAPID_SUBJECT, else the base URL's origin when it is https without a port, else a
    mailto placeholder (Apple may refuse that one: set the subject for iPhones)."""
    url = urlparse(base_url)
    https = url.scheme == "https" and url.hostname and url.port is None
    subject = env.get("SWARMIFY_VAPID_SUBJECT") or (
        f"https://{url.hostname}" if https else "mailto:swarmify@localhost"
    )
    public, private = env.get("SWARMIFY_VAPID_PUBLIC_KEY"), env.get("SWARMIFY_VAPID_PRIVATE_KEY")
    if public and private:
        return Keys(public, private, subject)
    file = data_dir / "vapid.json"
    if not file.exists():
        public, private = generate_keys()
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"public": public, "private": private}, f)
    saved = json.loads(file.read_text())
    return Keys(saved["public"], saved["private"], subject)


@dataclass(frozen=True)
class Target:
    endpoint: str
    p256dh: str
    auth: str


Send = Callable[[Target, bytes], int]
"""POST one encrypted payload; returns the push service's HTTP status."""


def sender(keys: Keys) -> Send:
    """The real Send: encrypt for the subscription, sign for its push service, POST."""
    vapid = Vapid02.from_string(private_key=keys.private)

    def send(target: Target, payload: bytes) -> int:
        url = urlparse(target.endpoint)
        claims = {
            "aud": f"{url.scheme}://{url.netloc}",
            "exp": int(time.time()) + 12 * 3600,
            "sub": keys.subject,
        }
        body = http_ece.encrypt(
            payload,
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=_unb64(target.p256dh),
            auth_secret=_unb64(target.auth),
            version="aes128gcm",
        )
        headers = {
            **vapid.sign(claims),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL),
            "Urgency": "high",
        }
        req = urllib.request.Request(target.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            return exc.code

    return send


@dataclass(frozen=True)
class Message:
    """What the service worker shows. `url` is a path in the web skin; `tag` makes a later
    message about the same gate or run replace this one; `renotify` buzzes again on replace."""

    kind: str
    title: str
    body: str
    url: str
    tag: str
    renotify: bool = False


def message(event: Event, gate_id: str | None) -> Message | None:
    """The push for a run event, or None when it is not one. `gate_id` is the open gate's stage
    id, for the deep link; a gate.opened without one was answered before it could be sent."""
    p = event.payload
    name = str(p.get("name") or "A run")
    run = p.get("run_id")
    run_url = f"/worlds/{event.world_id}/runs/{run}"
    if event.type == "gate.opened":
        if gate_id is None:
            return None
        url, tag = f"/gates?gate={gate_id}", f"gate-{run}"
        if p.get("kind") == "budget":
            body = p.get("ask") or "Its budget is spent. Add money or abort."
            return Message("budget", f"{name} is paused", body, url, tag, True)
        body = p.get("ask") or f"{p.get('stage') or 'A gate'} is waiting on you."
        return Message("gates", f"{name} needs you", body, url, tag, True)
    if event.type == "budget.warning":
        spent, limit = float(p["spent_usd"]), float(p["budget_usd"])
        body = f"Spent ${spent:.2f} of ${limit:.2f}. It pauses at 100 percent."
        return Message("budget", name, body, run_url, f"run-{run}")
    if event.type == "run.ended" and p.get("status") == "failed":
        return Message(
            "failures", f"{name} failed", "Open the run to see why.", run_url, f"run-{run}"
        )
    return None


@dataclass(frozen=True)
class Notice:
    """A Needs-you notice as the API lists it."""

    kind: str
    text: str
    world_id: str | None = None
    run_id: str | None = None


def _notice_key(n: Notice) -> str:
    # Digits stripped: a notice whose minutes tick up is the same notice.
    return f"{n.kind}|{n.world_id}|{n.run_id}|{re.sub(r'[0-9.]+', '', n.text)}"


class WebPush:
    """The Notifier for browsers. `gate_id(run_id, stage_index)` is the stage id of that run's
    gate while it is still open, else None."""

    def __init__(
        self,
        storage: PushStorage,
        send: Send,
        gate_id: Callable[[str, int], str | None] = lambda _r, _i: None,
    ) -> None:
        self.storage = storage
        self.send = send
        self.gate_id = gate_id
        # ponytail: in-memory, so a restart forgets which gates and notices were pushed. A replayed
        # save after a restart could push a gate twice; the tag makes the second replace the first.
        self.sent: set[tuple[str, int, int]] = set()
        self.seen: set[str] | None = None
        self.jobs: Queue[Callable[[], None]] = Queue()
        threading.Thread(target=self._work, name="web-push", daemon=True).start()

    # Subscriptions

    def subscribe(self, endpoint: str, p256dh: str, auth: str, label: str) -> dict:
        """Store this browser's subscription (again, if it changed keys). ValueError for an
        endpoint that is not https or keys that are not base64url."""
        url = urlparse(endpoint)
        if url.scheme != "https" or not url.netloc:
            raise ValueError("the push endpoint must be an https URL")
        try:
            if len(_unb64(p256dh)) != 65 or len(_unb64(auth)) != 16:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("the subscription keys are not valid") from exc
        id = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
        self.storage.save(id, endpoint, p256dh, auth, label.strip()[:80] or "A browser")
        return next(d for d in self.devices() if d["id"] == id)

    def devices(self) -> list[dict]:
        return [{**d, **{k: bool(d[k]) for k in KINDS}} for d in self.storage.devices()]

    def toggle(self, id: str, kinds: dict[str, bool]) -> bool:
        return self.storage.toggle(id, {k: v for k, v in kinds.items() if k in KINDS})

    def unsubscribe(self, id: str) -> bool:
        return self.storage.delete(id)

    # Sending

    def notify(self, event: Event) -> None:
        """Hand the event to the push thread; never blocks, never raises."""
        if event.type in ("gate.opened", "budget.warning", "run.ended"):
            self.jobs.put(lambda: self._event(event))

    def notices(self, current: Iterable[Notice]) -> None:
        """Push each notice that was not in the last list. The first list only sets the baseline,
        so a restart does not push every standing notice again."""
        notices = {_notice_key(n): n for n in current}
        before, self.seen = self.seen, set(notices)
        if before is None:
            return
        for key, n in notices.items():
            if key in before:
                continue
            url = f"/worlds/{n.world_id}/runs/{n.run_id}" if n.run_id else "/gates"
            tag = "notice-" + hashlib.sha256(key.encode()).hexdigest()[:12]
            text = n.text if len(n.text) <= 180 else n.text[:177] + "..."
            msg = Message("notices", "Needs you", text, url, tag)
            self.jobs.put(lambda msg=msg: self._push(msg))

    def test(self, id: str) -> int:
        """Send a test to one device now. Returns its push service's status; KeyError when there
        is no such device."""
        target = next((t for t in self.storage.targets(None) if t["id"] == id), None)
        if target is None:
            raise KeyError(id)
        msg = Message("test", "Swarmify", "Notifications are on for this device.", "/gates", "test")
        return self._one(target, msg)

    def flush(self) -> None:
        """Wait until every send handed over so far is done."""
        self.jobs.join()

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                log.exception("a web push failed")
            finally:
                self.jobs.task_done()

    def _event(self, event: Event) -> None:
        p = event.payload
        gate = None
        if event.type == "gate.opened":
            key = (str(p.get("run_id")), int(p.get("stage_index") or 0), int(p.get("visit") or 0))
            if key in self.sent:
                return
            self.sent.add(key)
            gate = self.gate_id(key[0], key[1])
        msg = message(event, gate)
        if msg is not None:
            self._push(msg)

    def _push(self, msg: Message) -> None:
        for target in self.storage.targets(msg.kind):
            self._one(target, msg)

    def _one(self, target: dict, msg: Message) -> int:
        payload = json.dumps({k: v for k, v in asdict(msg).items() if k != "kind"}).encode()
        try:
            status = self.send(
                Target(target["endpoint"], target["p256dh"], target["auth"]), payload
            )
        except OSError as exc:
            log.warning(
                "web push to device %s did not go out: %s", target["id"], type(exc).__name__
            )
            return 0
        if status in (404, 410):  # the browser dropped the subscription
            self.storage.delete(target["id"])
        elif 200 <= status < 300:
            self.storage.delivered(target["id"])
        else:
            log.warning("push service refused device %s: HTTP %s", target["id"], status)
        return status
