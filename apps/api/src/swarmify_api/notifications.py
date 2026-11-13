"""HTTP face of the Notifications context: the world's Slack Channel on the settings page, and
Web Push to the operator's browsers (issue 459)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, StringConstraints

from swarmify_notifications import Notifications
from swarmify_notifications.push import WebPush
from swarmify_worlds import Worlds


class ChannelUpdate(BaseModel):
    slack_webhook: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    """A Slack incoming webhook URL. Blank removes it."""


class ChannelView(BaseModel):
    slack_webhook: str | None
    """The stored webhook with its secret cut to the last four characters, or None. The full
    URL is a credential and is never returned."""


def masked(url: str | None) -> str | None:
    if url is None:
        return None
    scheme, _, rest = url.partition("://")
    host, _, path = rest.partition("/")
    return f"{scheme}://{host}/...{path[-4:]}"


def router(notifications: Notifications, worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api/worlds/{world_id}/notifications", tags=["notifications"])

    def world_or_404(world_id: str) -> None:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")

    @api.get("")
    def get_channel(world_id: str) -> ChannelView:
        world_or_404(world_id)
        return ChannelView(slack_webhook=masked(notifications.webhook(world_id)))

    @api.put("")
    def set_channel(world_id: str, body: ChannelUpdate) -> ChannelView:
        world_or_404(world_id)
        try:
            notifications.set_webhook(world_id, body.slack_webhook)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return get_channel(world_id)

    @api.post("/test", status_code=204)
    def send_test(world_id: str) -> Response:
        """Post a test message to the world's Slack. 409 with no webhook, 502 when Slack refuses."""
        world_or_404(world_id)
        try:
            notifications.send_test(world_id)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:  # urllib's HTTPError and URLError are both OSError
            raise HTTPException(502, f"Slack did not take the message: {exc}") from exc
        return Response(status_code=204)

    return api


class PushKeys(BaseModel):
    p256dh: Annotated[str, StringConstraints(max_length=200)]
    auth: Annotated[str, StringConstraints(max_length=200)]


class Subscribe(BaseModel):
    """A browser's PushSubscription as `toJSON()` gives it, and a name for the device."""

    endpoint: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
    keys: PushKeys
    label: Annotated[str, StringConstraints(max_length=200)] = ""


class Device(BaseModel):
    """A subscribed browser. Its endpoint is a credential and is never returned."""

    id: str
    label: str
    gates: bool
    failures: bool
    budget: bool
    notices: bool
    created_at: str
    last_success_at: str | None


class Toggles(BaseModel):
    gates: bool | None = None
    failures: bool | None = None
    budget: bool | None = None
    notices: bool | None = None


class PushKey(BaseModel):
    public_key: str
    """The VAPID public key: the browser's applicationServerKey."""


def push_router(web_push: WebPush, public_key: str) -> APIRouter:
    """Web Push on this server: the key browsers subscribe with, the subscribed devices, a test."""
    api = APIRouter(prefix="/api/push", tags=["notifications"])

    @api.get("/key")
    def key() -> PushKey:
        return PushKey(public_key=public_key)

    @api.get("/subscriptions")
    def devices() -> list[Device]:
        return [Device(**d) for d in web_push.devices()]

    @api.post("/subscriptions")
    def subscribe(body: Subscribe) -> Device:
        """Turn notifications on for this browser; again with the same endpoint refreshes it."""
        try:
            device = web_push.subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, body.label)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return Device(**device)

    @api.patch("/subscriptions/{device_id}")
    def toggle(device_id: str, body: Toggles) -> Device:
        if not web_push.toggle(device_id, body.model_dump(exclude_none=True)):
            raise HTTPException(404, "no such device")
        return next(Device(**d) for d in web_push.devices() if d["id"] == device_id)

    @api.delete("/subscriptions/{device_id}", status_code=204)
    def unsubscribe(device_id: str) -> Response:
        if not web_push.unsubscribe(device_id):
            raise HTTPException(404, "no such device")
        return Response(status_code=204)

    @api.post("/test/{device_id}", status_code=204)
    def test(device_id: str) -> Response:
        """Send a test to one device. 404 when it is gone (the push service said so), 502 when
        the push service refused or could not be reached."""
        try:
            status = web_push.test(device_id)
        except KeyError as exc:
            raise HTTPException(404, "no such device") from exc
        if status in (404, 410):
            raise HTTPException(404, "the browser has dropped this subscription; turn it on again")
        if not 200 <= status < 300:
            raise HTTPException(502, f"the push service did not take it (HTTP {status or 'none'})")
        return Response(status_code=204)

    return api
