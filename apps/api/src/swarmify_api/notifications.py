"""HTTP face of the Notifications context: the world's Slack Channel on the settings page."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, StringConstraints

from swarmify_notifications import Notifications
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
    return f"{scheme}://{rest.split('/')[0]}/...{url[-4:]}"


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
