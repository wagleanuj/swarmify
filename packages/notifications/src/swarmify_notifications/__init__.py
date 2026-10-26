"""Notifications context: tell a person that a run needs them or has ended, so gates are never
missed. A world's Channel is a Slack incoming webhook (v1); every Notification carries a deep
link into the web skin at `base_url`.

`Notifications` is the Slack adapter of the Runs Notifier port (ADR 0012). It satisfies the
port by shape, so this package imports nothing of Runs.
"""

import logging
from urllib.parse import urlparse

from swarmify_kernel import Event
from swarmify_notifications import slack
from swarmify_notifications.ports import Storage
from swarmify_notifications.sqlite import MIGRATIONS, SqliteStorage

__all__ = ["MIGRATIONS", "Notifications", "SqliteStorage", "Storage", "slack"]

log = logging.getLogger("swarmify.notify")


class Notifications:
    def __init__(self, storage: Storage, base_url: str) -> None:
        self.storage = storage
        self.base_url = base_url

    def webhook(self, world_id: str) -> str | None:
        return self.storage.get_webhook(world_id)

    def set_webhook(self, world_id: str, url: str | None) -> None:
        """Store the world's Slack webhook; None or blank removes it. Raises ValueError for
        anything but an http(s) URL."""
        url = (url or "").strip() or None
        if url is not None:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("the webhook must be an https URL from Slack")
        self.storage.set_webhook(world_id, url)

    def notify(self, event: Event) -> None:
        """Post the event's message to its world's Channel. Silent when the world has none or
        the event is not a Notification. Raises when Slack refuses; the caller logs and goes on."""
        text = slack.message(event, self.base_url)
        if text is None:
            return
        log.info("%s %s", event.type, event.payload)
        webhook = self.storage.get_webhook(event.world_id)
        if webhook:
            slack.post(webhook, text)

    def send_test(self, world_id: str) -> None:
        """Post a test message. LookupError when the world has no webhook."""
        webhook = self.storage.get_webhook(world_id)
        if not webhook:
            raise LookupError("no Slack webhook is set for this world")
        gates = f"{self.base_url.rstrip('/')}/gates"
        slack.post(webhook, f"Swarmify is connected. <{gates}|Open gates>")

    def delete_world(self, world_id: str) -> None:
        self.storage.set_webhook(world_id, None)
