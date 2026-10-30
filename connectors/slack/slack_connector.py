"""The Slack connector: Remote Approvals in Slack (spec V2, Capabilities).

As a stdio MCP server it answers the `remote_approvals` Capability the core owns: `post_gate`
posts a gate to the world's channel with a Block Kit button per answer the core offers, and
`update_gate` replaces the buttons with how the gate closed, wherever it was answered. Run as
`slack_connector.py listen`, it is the long-lived process the core keeps running: a Bolt app in
Socket Mode (an outbound websocket, so Swarmify needs no public URL) that takes each button
click and answers the gate through the core's MCP server, with the token the core gave it, as
the Slack user who clicked. Only `web` and `decide` reach the outside; tests replace them.
Slack's SDK and Bolt live in this package's own environment (ADR 0026), so they are imported
where they are used and the core's checks do not resolve them.
"""

import json
import os
import re
import sys
from collections.abc import Callable
from typing import Any

import anyio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.shared._httpx_utils import create_mcp_http_client

ACTION = "swarmify_answer"
TEXT = 3000
"""Slack's limit on a section's text."""
mcp = MCPServer("slack")


def web(token_env: str = "SLACK_BOT_TOKEN") -> Any:
    """The Slack Web API client, signed in with the Setup's bot token (or another of its)."""
    from slack_sdk import WebClient  # pyright: ignore[reportMissingImports]

    return WebClient(token=os.environ[token_env])


def _esc(text: object) -> str:
    """Slack mrkdwn treats &, < and > as control characters."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:TEXT]}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def blocks(gate: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The gate's message: who waits where and why, what a publish would send, a button per
    answer the core offers, and a link to the web UI for notes, edits and every other answer."""
    head = f"*{_esc(gate['run_name'])}* is waiting on you at *{_esc(gate['stage'])}*."
    out = [_section(f"{head}\n{_esc(gate['ask'])}" if gate.get("ask") else head)]
    if gate.get("publish"):
        lines = [f"*{_esc(f['label'])}*: {_esc(f['value'] or '')}" for f in gate["publish"]]
        out.append(_section("\n".join(lines)))
    buttons = []
    for i, answer in enumerate(gate.get("answers") or []):
        value = {k: gate[k] for k in ("run_id", "stage_index", "visit")} | {
            "decide": answer["decide"]
        }
        button = {
            "type": "button",
            "action_id": f"{ACTION}_{i}",
            "text": {"type": "plain_text", "text": answer["label"][:75]},
            "value": json.dumps(value)[:2000],
        }
        if answer.get("primary"):
            button["style"] = "primary"
        buttons.append(button)
    if buttons:
        out.append({"type": "actions", "block_id": "answers", "elements": buttons[:25]})
    rest = "to add a note or answer another way" if buttons else "to answer it"
    out.append(_context(f"<{gate['link']}|Open it in Swarmify> {rest}."))
    return f"{gate['run_name']} is waiting on you at {gate['stage']}.", out


@mcp.tool(description="Post an opened gate to the world's channel. Answers its message_ref.")
def post_gate(gate: dict[str, Any]) -> dict[str, Any]:
    text, shown = blocks(gate)
    sent = web().chat_postMessage(channel=os.environ["SLACK_CHANNEL"], text=text, blocks=shown)
    head = shown[0]["text"]["text"].split("\n")[0]
    link = gate["link"]
    return {
        "message_ref": {"channel": sent["channel"], "ts": sent["ts"], "head": head, "link": link}
    }


@mcp.tool(description="Replace a posted gate's buttons with how it closed.")
def update_gate(message_ref: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    said = _esc(resolution["text"])
    shown = [
        _section(f"{message_ref['head']}\n{said}"),
        _context(f"<{message_ref['link']}|Open the run in Swarmify>."),
    ]
    web().chat_update(
        channel=message_ref["channel"], ts=message_ref["ts"], text=resolution["text"], blocks=shown
    )
    return {"ok": True}


@mcp.tool(description="The Setup test: both tokens sign in, the app's in Socket Mode.")
def check_setup() -> dict[str, Any]:
    from slack_sdk.errors import SlackApiError  # pyright: ignore[reportMissingImports]

    try:
        who = web().auth_test()
        web("SLACK_APP_TOKEN").apps_connections_open()
    except SlackApiError as exc:
        return {"ok": False, "message": f"Slack refused it: {exc.response.get('error')}"}
    return {"ok": True, "message": f"Signed in to {who['team']} as {who['user']}."}


def answer(body: dict[str, Any], decide: Callable[[dict[str, Any]], str | None]) -> str | None:
    """Answer the gate a clicked button is for, as the Slack user who clicked. None when it was
    answered, else why not, which only the clicker sees (it was answered already, say)."""
    value = json.loads(body["actions"][0]["value"])
    user = body.get("user") or {}
    who = user.get("username") or user.get("name") or user.get("id") or "someone"
    arguments = {k: value[k] for k in ("run_id", "stage_index", "visit")}
    return decide({**arguments, "by": f"{who} in Slack", **value["decide"]})


def decide(arguments: dict[str, Any]) -> str | None:
    """Call `decide_gate` on the core's MCP server; None when it took the answer."""
    headers = {"Authorization": f"Bearer {os.environ['SWARMIFY_API_TOKEN']}"}

    async def go() -> str | None:
        async with (
            create_mcp_http_client(headers=headers) as http,
            Client(
                streamable_http_client(os.environ["SWARMIFY_MCP_URL"], http_client=http)
            ) as core,
        ):
            result = await core.call_tool("decide_gate", arguments)
            said = "\n".join(getattr(b, "text", "") for b in result.content).strip()
            said = said.removeprefix("Error executing tool decide_gate: ")
            return (said or "Swarmify refused it.") if result.is_error else None

    try:
        return anyio.run(go)
    except Exception as exc:
        return f"Swarmify could not be reached: {exc}"


def listen() -> None:
    """The Socket Mode app: each button click answers its gate; a refusal is shown only to the
    person who clicked. The message itself is updated by the core, through `update_gate`."""
    from slack_bolt import App  # pyright: ignore[reportMissingImports]
    from slack_bolt.adapter.socket_mode import SocketModeHandler  # pyright: ignore[reportMissingImports]

    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    @app.action(re.compile(f"^{ACTION}_"))
    def clicked(ack: Any, body: dict[str, Any], respond: Any) -> None:
        ack()
        if refused := answer(body, decide):
            respond(
                text=f"Not answered: {refused}", response_type="ephemeral", replace_original=False
            )

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    if sys.argv[1:] == ["listen"]:
        listen()
    else:
        mcp.run("stdio")
