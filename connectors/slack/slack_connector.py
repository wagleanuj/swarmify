"""The Slack connector: Remote Approvals in Slack (spec V2, Capabilities).

As a stdio MCP server it answers the `remote_approvals` Capability the core owns: `post_gate`
posts a gate to the world's channel with a Block Kit button per answer the core offers, and
`update_gate` replaces the buttons with how the gate closed, wherever it was answered. Run as
`slack_connector.py listen`, it is the long-lived process the core keeps running: a Bolt app in
Socket Mode (an outbound websocket, so Swarmify needs no public URL) that takes each button
click and answers the gate through the core's MCP server, with the token the core gave it, as
the Slack user who clicked. A reply in a gate's thread is a turn of the run's Run Assistant (the
`run_assistant` Capability), its answer posted back in the thread with Apply and Not now on each
proposal. Only `web` and `core` reach the outside; tests replace them.
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
APPLY, NOT_NOW = "swarmify_apply", "swarmify_not_now"
GATE = "swarmify_gate"
"""The metadata event type of a posted gate, naming its run for the replies in its thread."""
TEXT = 3000
"""Slack's limit on a section's text."""
SHORT = 600
"""The most of an answer a thread shows; the run page has the rest."""
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
    link = gate["link"]
    sent = web().chat_postMessage(
        channel=os.environ["SLACK_CHANNEL"],
        text=text,
        blocks=shown,
        metadata={"event_type": GATE, "event_payload": {"run_id": gate["run_id"], "link": link}},
    )
    head = shown[0]["text"]["text"].split("\n")[0]
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


class Refused(Exception):
    """The core refused a call, or could not be reached; the message says why."""


def _who(body: dict[str, Any]) -> str:
    user = body.get("user") or {}
    return user.get("username") or user.get("name") or user.get("id") or "someone"


def answer(body: dict[str, Any], decide: Callable[[dict[str, Any]], str | None]) -> str | None:
    """Answer the gate a clicked button is for, as the Slack user who clicked. None when it was
    answered, else why not, which only the clicker sees (it was answered already, say)."""
    value = json.loads(body["actions"][0]["value"])
    arguments = {k: value[k] for k in ("run_id", "stage_index", "visit")}
    return decide({**arguments, "by": f"{_who(body)} in Slack", **value["decide"]})


def core(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the core's MCP server and answer what it returned. Raises Refused."""
    headers = {"Authorization": f"Bearer {os.environ['SWARMIFY_API_TOKEN']}"}

    async def go() -> tuple[dict[str, Any], str | None]:
        async with (
            create_mcp_http_client(headers=headers) as http,
            Client(
                streamable_http_client(os.environ["SWARMIFY_MCP_URL"], http_client=http)
            ) as swarmify,
        ):
            result = await swarmify.call_tool(tool, arguments)
            said = "\n".join(getattr(b, "text", "") for b in result.content).strip()
            said = said.removeprefix(f"Error executing tool {tool}: ")
            refused = (said or "Swarmify refused it.") if result.is_error else None
            return result.structured_content or {}, refused

    try:
        out, refused = anyio.run(go)
    except Exception as exc:
        raise Refused(f"Swarmify could not be reached: {exc}") from exc
    if refused:
        raise Refused(refused)
    return out


def decide(arguments: dict[str, Any]) -> str | None:
    """Call `decide_gate` on the core's MCP server; None when it took the answer."""
    try:
        core("decide_gate", arguments)
    except Refused as exc:
        return str(exc)
    return None


def thread_gate(event: dict[str, Any]) -> dict[str, Any] | None:
    """The run and link of the gate a message replies to in its thread, from the metadata
    `post_gate` put on it; None for anything else: a message of its own, the app's, an edit."""
    thread = event.get("thread_ts")
    if event.get("bot_id") or event.get("subtype") or thread in (None, event.get("ts")):
        return None
    found = web().conversations_replies(
        channel=event["channel"], ts=thread, limit=1, include_all_metadata=True
    )
    meta = next(iter(found.get("messages") or []), {}).get("metadata") or {}
    return meta.get("event_payload") if meta.get("event_type") == GATE else None


def _button(action: str, label: str, value: str) -> dict[str, Any]:
    text = {"type": "plain_text", "text": label}
    button: dict[str, Any] = {"type": "button", "action_id": action, "text": text, "value": value}
    if action == APPLY:
        button["style"] = "primary"
    return button


def answered(said: dict[str, Any], link: str) -> tuple[str, list[dict[str, Any]]]:
    """The assistant's answer as a thread reply: short, each proposal with Apply (unless the
    check would refuse it) and Not now, and a link to the run page."""
    reply = str(said.get("reply") or "").strip() or "(no answer)"
    short = reply if len(reply) <= SHORT else reply[:SHORT].rsplit(" ", 1)[0] + "..."
    out = [_section(_esc(short))]
    for proposal in said.get("proposals") or []:
        problems = "".join(f"\nThis would not pass: {_esc(v)}." for v in proposal["violations"])
        out.append(_section(f"*{_esc(proposal['title'])}*{problems}{consequences(proposal)}"))
        value = json.dumps({"run_id": said["run_id"], "proposal_id": proposal["id"]})
        not_now = _button(NOT_NOW, "Not now", value)
        shown = [not_now] if proposal["violations"] else [_button(APPLY, "Apply", value), not_now]
        out.append({"type": "actions", "block_id": proposal["id"], "elements": shown})
    rest = "Read it all in Swarmify" if short != reply else "Open the run in Swarmify"
    out.append(_context(f"<{link}|{rest}>."))
    return short, out


def consequences(proposal: dict[str, Any]) -> str:
    """What applying a proposal costs, as its card on the run page says it: who already read
    the old file, what a Rerun resets for how much, and a Fork ending this run."""
    said = ""
    if read_by := proposal.get("read_by"):
        said += f"\n{_esc(', '.join(read_by))} already read the old version."
    if proposal.get("resets") and (cost := proposal.get("cost_usd")) is not None:
        left = proposal.get("budget_left_usd")
        of = f" of the ${left:.2f} left" if left is not None else ""
        said += f"\nResets {_esc(', '.join(proposal['resets']))}, about ${cost:.2f} more{of}."
    if proposal.get("in_place") is False:
        said += f"\nContinues as a new run from {_esc(str(proposal['fork_from']))}; this one ends."
    return said


def reply(event: dict[str, Any], call: Callable[[str, dict[str, Any]], dict[str, Any]]) -> None:
    """A plain-text reply in a gate's thread: a turn of the run's Run Assistant, its answer
    posted back in the thread."""
    gate = thread_gate(event)
    if gate is None or not str(event.get("text") or "").strip():
        return
    try:
        said = call("ask_run", {"run_id": gate["run_id"], "message": event["text"]})
        text, shown = answered({**said, "run_id": gate["run_id"]}, gate["link"])
    except Refused as exc:
        text = str(exc)
        shown = [_section(_esc(text)), _context(f"<{gate['link']}|Open the run in Swarmify>.")]
    thread = {"channel": event["channel"], "thread_ts": event["thread_ts"]}
    web().chat_postMessage(**thread, text=text, blocks=shown)


def settle(
    body: dict[str, Any], call: Callable[[str, dict[str, Any]], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply or dismiss the proposal a clicked button is for, as the Slack user who clicked:
    the message's blocks with that proposal's buttons replaced by what became of it. Raises
    Refused with why not (the run moved on, say), which only the clicker sees."""
    action, who = body["actions"][0], _who(body)
    value = json.loads(action["value"])
    if action["action_id"] == APPLY:
        call("apply_proposal", {**value, "by": f"slack:{who}"})
        said = f"Applied by {who}."
    else:
        call("dismiss_proposal", value)
        said = f"Not now, said {who}."
    return [
        _context(said) if b.get("block_id") == action["block_id"] else b
        for b in body["message"]["blocks"]
    ]


def listen() -> None:
    """The Socket Mode app: each button click answers its gate; a refusal is shown only to the
    person who clicked. The message itself is updated by the core, through `update_gate`. A
    reply in a gate's thread asks the run's assistant; Apply and Not now settle its proposals."""
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

    @app.action(re.compile(f"^({APPLY}|{NOT_NOW})$"))
    def settled(ack: Any, body: dict[str, Any], respond: Any) -> None:
        ack()
        try:
            shown = settle(body, core)
        except Refused as exc:
            respond(text=str(exc), response_type="ephemeral", replace_original=False)
            return
        respond(text=body["message"].get("text", ""), blocks=shown, replace_original=True)

    @app.event("message")
    def replied(event: dict[str, Any]) -> None:
        reply(event, core)

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    if sys.argv[1:] == ["listen"]:
        listen()
    else:
        mcp.run("stdio")
