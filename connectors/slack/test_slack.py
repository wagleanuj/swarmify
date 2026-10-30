"""The Slack connector against a fake Slack client, never Slack itself: the message a gate
posts, its update once the gate closes, and a click answering the gate through the core's MCP
server, run for real on DBOS behind a live API with the package installed through the real
install path."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import slack_connector as sc
import uvicorn
from fastapi import FastAPI

from swarmify_api.auth import Operator
from swarmify_api.main import create_app
from swarmify_connectors import Connector, FakeTransport, McpTransport
from swarmify_notifications.fake import FakeSlack as FakeWebhook

PASSWORD = "correct horse"
CHANNEL = "C0123ABCDEF"
DRAFT = """name: Draft
stages:
  - connector: draft
    calls: remotion_render
    params: {project: /tmp, composition: Short}
  - gate: review
    ask: Approve or send back.
    kind: approve
  - gate: ok
    ask: Send it?
    kind: confirm
"""


class FakeSlack:
    """The Slack Web API client's fake: records each post and update, as Slack answers them."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def chat_postMessage(self, **message: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(message)
        return {"ok": True, "channel": message["channel"], "ts": f"{len(self.posted)}.000100"}

    def chat_update(self, **message: Any) -> dict[str, Any]:
        self.updated.append(message)
        return {"ok": True}


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 15
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def buttons(message: dict[str, Any]) -> list[dict[str, Any]]:
    return next((b["elements"] for b in message["blocks"] if b["type"] == "actions"), [])


def click(button: dict[str, Any], who: str = "anuj") -> str | None:
    return sc.answer({"user": {"id": "U1", "username": who}, "actions": [button]}, sc.decide)


GATE = {
    "run_id": "run_1",
    "run_name": "Weekly <short>",
    "world_id": "w",
    "stage_index": 3,
    "visit": 1,
    "stage": "review",
    "kind": "approve",
    "ask": "Approve or send back.",
    "link": "http://swarmify.lan/worlds/w/runs/run_1",
    "publish": [{"label": "Title", "value": "Five hooks"}],
    "answers": [
        {"label": "Approve", "primary": True, "decide": {}},
        {"label": "Send back to draft", "decide": {"send_back_to": "draft"}},
    ],
}


def test_a_gate_posts_a_button_per_answer_and_its_update_takes_them_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack = FakeSlack()
    monkeypatch.setattr(sc, "web", lambda *_: slack)
    monkeypatch.setenv("SLACK_CHANNEL", CHANNEL)
    ref = sc.post_gate(GATE)["message_ref"]
    (posted,) = slack.posted
    assert posted["channel"] == CHANNEL and ref["channel"] == CHANNEL and ref["ts"] == "1.000100"
    assert posted["text"] == "Weekly <short> is waiting on you at review."
    head = posted["blocks"][0]["text"]["text"]
    assert head == "*Weekly &lt;short&gt;* is waiting on you at *review*.\nApprove or send back."
    assert posted["blocks"][1]["text"]["text"] == "*Title*: Five hooks"
    approve, back = buttons(posted)
    assert approve["text"]["text"] == "Approve" and approve["style"] == "primary"
    assert back["text"]["text"] == "Send back to draft" and "style" not in back
    assert "add a note or answer another way" in posted["blocks"][-1]["elements"][0]["text"]

    asked: list[dict[str, Any]] = []
    monkeypatch.setattr(sc, "decide", lambda args: asked.append(args) or "already answered")
    assert click(back, "lee") == "already answered"
    assert asked == [
        {
            "run_id": "run_1",
            "stage_index": 3,
            "visit": 1,
            "by": "lee in Slack",
            "send_back_to": "draft",
        }
    ]

    sc.update_gate(ref, {"outcome": "approved", "text": "Approved by lee in Slack."})
    (updated,) = slack.updated
    assert (updated["channel"], updated["ts"]) == (CHANNEL, "1.000100")
    assert updated["text"] == "Approved by lee in Slack." and buttons(updated) == []
    assert updated["blocks"][0]["text"]["text"].endswith("*review*.\nApproved by lee in Slack.")


class InProcess(FakeTransport):
    """Answers the Slack package's tools with the module itself, so its fake client stands in
    for Slack; the Setup test, which would ask Slack, passes."""

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        if connector.name != "slack":
            return super().call(connector, tool, arguments, env)
        self.calls.append((connector.name, tool, arguments, env))
        if tool == "check_setup":
            return {"ok": True, "message": "Signed in."}
        return getattr(sc, tool)(**arguments)


@contextmanager
def served(app: FastAPI) -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until(lambda: server.started)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    fake = FakeSlack()
    monkeypatch.setattr(sc, "web", lambda *_: fake)
    monkeypatch.setenv("SLACK_CHANNEL", CHANNEL)
    monkeypatch.setenv("SWARMIFY_PASSWORD", PASSWORD)
    monkeypatch.setenv("SWARMIFY_BASE_URL", "http://swarmify.lan")
    return fake


def test_slack_and_the_web_ui_answer_gates_alike_and_both_update_the_message(
    tmp_path: Path, slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = InProcess()
    # No sweep after the first: it would start the Socket Mode listener, which dials Slack.
    app = create_app(
        tmp_path, transport=transport, connector=lambda *_: None, retro=False, sweep_seconds=3600
    )
    with FakeWebhook() as webhook, served(app) as url, httpx.Client(base_url=url) as http:
        http.post("/api/session", json={"password": PASSWORD}).raise_for_status()
        world = http.post("/api/worlds", json={"name": "Posts"}).json()["id"]
        http.put(f"/api/worlds/{world}/notifications", json={"slack_webhook": webhook.url})
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": DRAFT}).json()
        start = f"/api/workflows/{wf['workflow_id']}/runs"

        def at(run_id: str, stage: str) -> bool:
            run = http.get(f"/api/runs/{run_id}").json()
            return any(s["name"] == stage and s["status"] == "waiting" for s in run["stages"])

        # Until the world sets the package up, V1's webhook stays the Channel.
        first = http.post(start, json={}).json()["id"]
        until(lambda: any("waiting on you at gate review" in t for t in webhook.texts()))
        assert http.post(f"/api/runs/{first}/abort").status_code == 200

        assert "slack" not in http.get(f"/api/worlds/{world}/packages").json()  # not by default
        enabled = http.put(f"/api/worlds/{world}/packages/slack", json={"version": "1.0.0"})
        assert enabled.status_code == 200
        setup = {
            "shared": {"bot_token": "xoxb-1", "app_token": "xapp-1"},
            "world": {"channel": CHANNEL},
        }
        assert http.put(f"/api/worlds/{world}/packages/slack/setup", json=setup).status_code == 200
        hooked = len(webhook.posts)

        run = http.post(start, json={}).json()["id"]
        until(lambda: len(slack.posted) == 1)
        approve, back = buttons(slack.posted[0])
        assert back["text"]["text"] == "Send back to draft"
        posting = next(c for c in transport.calls if c[1] == "post_gate")
        assert posting[3]["SLACK_CHANNEL"] == CHANNEL
        assert posting[2]["gate"]["link"] == f"http://swarmify.lan/worlds/{world}/runs/{run}"

        # The connector answers with the token the core provisions for its process.
        token = Operator(tmp_path / "swarmify.db").provision("slack", "remote_approvals")
        monkeypatch.setenv("SWARMIFY_API_TOKEN", token)
        monkeypatch.setenv("SWARMIFY_MCP_URL", f"{url}/mcp/remote_approvals")
        whole = {"Authorization": f"Bearer {token}"}
        assert httpx.post(f"{url}/mcp", headers=whole, json={}).status_code == 401  # scoped

        assert click(approve) is None
        until(lambda: at(run, "ok") and len(slack.updated) == 1)
        assert slack.updated[0]["text"] == "Approved by anuj in Slack."
        assert http.get(f"/api/runs/{run}").json()["decisions"][0]["by"] == "anuj in Slack"
        assert click(approve) == "this gate was already answered"  # first answer won

        until(lambda: len(slack.posted) == 2)
        (confirm,) = buttons(slack.posted[1])
        assert http.post(f"/api/runs/{run}/decision", json={}).status_code == 200
        until(lambda: len(slack.updated) == 2)
        assert slack.updated[1]["text"] == "Confirmed in Swarmify."
        assert (slack.updated[1]["channel"], slack.updated[1]["ts"]) == (CHANNEL, "2.000100")
        assert click(confirm) == "this gate was already answered"
        # Gates went to Slack instead; the webhook keeps only run endings and budget warnings.
        assert not any("waiting on you" in t for t in webhook.texts()[hooked:])

    # The package came through the real install path, and its server starts over stdio.
    folder = tmp_path / "packages/slack/1.0.0"
    package = Connector(
        "slack", world, "slack", "notify", [".venv/bin/python", "slack_connector.py"],
        folder=str(folder),
    )  # fmt: skip
    names = {t.name for t in McpTransport().list_tools(package, {})}
    assert names == {"post_gate", "update_gate", "check_setup"}
