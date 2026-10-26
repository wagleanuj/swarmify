"""Slack messages: what each event says, and the incoming webhook post that sends it."""

import json
import urllib.request
from typing import Any

from swarmify_kernel import Event

TIMEOUT = 5.0


def _esc(text: object) -> str:
    """Slack mrkdwn treats &, < and > as control characters."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def message(event: Event, base_url: str) -> str | None:
    """The text for `event`, with a deep link into the web skin. None when it is not a
    Notification: gate opened (a budget pause is a gate of kind budget), budget warning,
    run ended."""
    p = event.payload
    base = base_url.rstrip("/")
    name = f"*{_esc(p.get('name') or p.get('run_id'))}*"
    run_link = f"{base}/worlds/{event.world_id}/runs/{p.get('run_id')}"
    if event.type == "gate.opened":
        ask = f" {_esc(p['ask'])}" if p.get("ask") else ""
        if p.get("kind") == "budget":
            return f"{name} is paused, its budget is spent.{ask} <{base}/gates|Add money or abort>"
        stage = _esc(p.get("stage") or "")
        return f"{name} is waiting on you at gate {stage}.{ask} <{base}/gates|Answer the gate>"
    if event.type == "budget.warning":
        spent, limit = float(p["spent_usd"]), float(p["budget_usd"])
        return (
            f"{name} has spent ${spent:.2f} of its ${limit:.2f} budget."
            f" It pauses at 100 percent. <{run_link}|Open the run>"
        )
    if event.type == "run.ended":
        said = {"done": "is done", "failed": "failed", "aborted": "was aborted"}
        status = str(p.get("status"))
        return f"{name} {said.get(status, _esc(status))}. <{run_link}|Open the run>"
    return None


def post(webhook: str, text: str) -> None:
    """POST `{"text": ...}` to a Slack incoming webhook. Raises on a non-2xx answer."""
    body: dict[str, Any] = {"text": text}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT):  # raises HTTPError on 4xx and 5xx
        pass
