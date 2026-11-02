"""The Slack connector against a fake Slack client, never Slack itself: the message a gate
posts, its update once the gate closes, a click answering the gate through the core's MCP
server, and a reply in its thread asking the run's assistant, whose proposals Slack applies, run
for real on DBOS behind a live API with the package installed through the real install path."""

import json
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
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import Answer, Call, Script

PASSWORD = "correct horse"
CHANNEL = "C0123ABCDEF"
DRAFT = """name: Draft
stages:
  - connector: draft
    calls: studio
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

    def conversations_replies(self, channel: str, ts: str, **_: Any) -> dict[str, Any]:
        """A thread, its first message first: the posted message whose ts it is."""
        found = [m for n, m in enumerate(self.posted, 1) if f"{n}.000100" == ts]
        return {"ok": True, "messages": found}


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


def replied(text: str, thread: str = "1.000100") -> dict[str, Any]:
    return {"channel": CHANNEL, "ts": "7.000200", "thread_ts": thread, "user": "U1", "text": text}


def pressed(message: dict[str, Any], proposal: str, label: str, who: str = "lee") -> dict:
    """What Bolt hands the app when `who` presses a proposal's button in the thread reply."""
    block = next(b for b in message["blocks"] if b.get("block_id") == proposal)
    button = next(e for e in block["elements"] if e["text"]["text"] == label)
    user = {"id": "U2", "username": who}
    return {"user": user, "actions": [{**button, "block_id": proposal}], "message": message}


def test_a_thread_reply_asks_the_runs_assistant_and_its_proposals_come_with_apply_and_not_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack = FakeSlack()
    monkeypatch.setattr(sc, "web", lambda *_: slack)
    monkeypatch.setenv("SLACK_CHANNEL", CHANNEL)
    sc.post_gate(GATE)
    calls: list[tuple[str, dict[str, Any]]] = []
    said = {
        "reply": "Because " + "the hook is strong. " * 60,
        "proposals": [
            {"id": "proposal_1", "title": "Edit render", "violations": []},
            {"id": "proposal_2", "title": "Skip review", "violations": ["it guards a post"]},
        ],
    }
    consequences = [
        {"read_by": ["render", "tag"]},
        {"resets": ["draft", "review"], "cost_usd": 0.4, "budget_left_usd": 2.5},
        {"in_place": False, "fork_from": "draft", "ends": True},
        {"in_place": False, "fork_from": "upload", "ends": False},
    ]
    said["proposals"] += [
        {"id": f"proposal_{n}", "title": "Change", "violations": [], **c}
        for n, c in enumerate(consequences, 3)
    ]

    def core(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool, arguments))
        if arguments.get("proposal_id") == "proposal_9":
            raise sc.Refused("The run moved on; open it to see the new proposal.")
        return said if tool == "ask_run" else {"state": "applied"}

    # Only a person's reply in a gate's thread is asked: not the app's own, nor a new message.
    sc.reply({**replied("hi"), "bot_id": "B1"}, core)
    sc.reply(replied("hi", thread="7.000200"), core)
    sc.reply({**replied("hi"), "thread_ts": None}, core)
    assert calls == [] and len(slack.posted) == 1

    sc.reply(replied("Why this hook?"), core)
    assert calls == [("ask_run", {"run_id": "run_1", "message": "Why this hook?"})]
    answer = slack.posted[-1]
    assert (answer["channel"], answer["thread_ts"]) == (CHANNEL, "1.000100")
    assert len(answer["text"]) <= sc.SHORT + 3 and answer["text"].endswith(" is...")
    shown = [b["text"]["text"] for b in answer["blocks"] if b["type"] == "section"]
    assert shown[1:] == [
        "*Edit render*",
        "*Skip review*\nThis would not pass: it guards a post.",
        "*Change*\nrender, tag already read the old version.",
        "*Change*\nResets draft, review, about $0.40 more of the $2.50 left.",
        "*Change*\nContinues as a new run from draft; this one ends.",
        "*Change*\nContinues as a new run from upload.",  # the run had already ended
    ]
    labels = [[e["text"]["text"] for e in b["elements"]] for b in answer["blocks"][2::2]][:2]
    assert labels == [["Apply", "Not now"], ["Not now"]]  # a refused change cannot be applied
    link = answer["blocks"][-1]["elements"][0]["text"]
    assert link == f"<{GATE['link']}|Read it all in Swarmify>."

    applied = sc.settle(pressed(answer, "proposal_1", "Apply"), core)
    by = {"run_id": "run_1", "proposal_id": "proposal_1", "by": "slack:lee"}
    assert calls[-1] == ("apply_proposal", by)
    assert applied[2] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Applied by lee."}],
    }
    assert applied[:2] == answer["blocks"][:2] and applied[3:] == answer["blocks"][3:]
    dismissed = sc.settle(pressed(answer, "proposal_2", "Not now", "kim"), core)
    assert calls[-1] == ("dismiss_proposal", {"run_id": "run_1", "proposal_id": "proposal_2"})
    assert dismissed[4]["elements"][0]["text"] == "Not now, said kim."

    stale = pressed(answer, "proposal_1", "Apply")
    stale["actions"][0]["value"] = json.dumps({"run_id": "run_1", "proposal_id": "proposal_9"})
    with pytest.raises(sc.Refused, match="The run moved on; open it to see the new proposal."):
        sc.settle(stale, core)

    said.update(reply="", proposals=[])
    sc.reply(replied("Anything?"), core)
    assert slack.posted[-1]["blocks"][0]["text"]["text"] == "(no answer)"  # never empty

    def down(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise sc.Refused("Swarmify could not be reached: refused")

    sc.reply(replied("Still there?"), down)
    assert slack.posted[-1]["text"] == "Swarmify could not be reached: refused"


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
        studio: dict[str, Any] = {"name": "studio", "kind": "stage", "server": "uvx studio"}
        studio["stage_types"] = ["connector"]  # a stand-in, never started
        http.post(f"/api/worlds/{world}/connectors", json=studio).raise_for_status()
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
        enabled = http.put(f"/api/worlds/{world}/packages/slack", json={"version": "1.1.0"})
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
    folder = tmp_path / "packages/slack/1.1.0"
    package = Connector(
        "slack", world, "slack", "notify", [".venv/bin/python", "slack_connector.py"],
        folder=str(folder),
    )  # fmt: skip
    names = {t.name for t in McpTransport().list_tools(package, {})}
    assert names == {"post_gate", "update_gate", "check_setup"}


def test_a_thread_reply_reaches_the_runs_assistant_and_slack_applies_its_proposal(
    tmp_path: Path, slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script()
    app = create_app(
        tmp_path, transport=InProcess(), connector=lambda *_: None, retro=False,
        sweep_seconds=3600, runtime=SdkRuntime(transport=script.transport),
    )  # fmt: skip
    with served(app) as url, httpx.Client(base_url=url) as http:
        http.post("/api/session", json={"password": PASSWORD}).raise_for_status()
        world = http.post("/api/worlds", json={"name": "Posts"}).json()["id"]
        studio: dict[str, Any] = {"name": "studio", "kind": "stage", "server": "uvx studio"}
        studio["stage_types"] = ["connector"]
        http.post(f"/api/worlds/{world}/connectors", json=studio).raise_for_status()
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": DRAFT}).json()
        token = Operator(tmp_path / "swarmify.db").provision("slack", "remote_approvals")
        monkeypatch.setenv("SWARMIFY_API_TOKEN", token)
        monkeypatch.setenv("SWARMIFY_MCP_URL", f"{url}/mcp/remote_approvals")

        def ask(run_id: str) -> None:
            sc.core("ask_run", {"run_id": run_id, "message": "hi"})

        # The core answers only a world that enables a connector offering run_assistant.
        unhooked = http.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()["id"]
        with pytest.raises(sc.Refused, match="no connector of this world offers"):
            ask(unhooked)
        http.post(f"/api/runs/{unhooked}/abort").raise_for_status()

        http.put(f"/api/worlds/{world}/packages/slack", json={"version": "1.1.0"})
        setup = {"shared": {"bot_token": "xoxb-1", "app_token": "xapp-1"}}
        setup["world"] = {"channel": CHANNEL}
        assert http.put(f"/api/worlds/{world}/packages/slack/setup", json=setup).is_success
        run_id = http.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()["id"]
        until(lambda: len(slack.posted) == 1)
        gate = slack.posted[0]
        run = http.get(f"/api/runs/{run_id}").json()
        ok = next(s for s in run["stages"] if s["name"] == "ok")
        edit = {"kind": "stage_edit", "stage": ok["id"]}
        edit["payload"] = {**ok["config"], "ask": "Ship it now?"}
        script.scripts = [
            [
                Call(tool_id("propose_change"), edit),
                Answer({"reply": "Here it is.", "sources": []}),
            ],
            [
                Call(
                    tool_id("propose_change"), {**edit, "payload": {**ok["config"], "ask": "Go?"}}
                ),
                Answer({"reply": "Or this.", "sources": []}),
            ],
        ]

        thread = {"channel": CHANNEL, "ts": "9.000100", "user": "U1", "thread_ts": "1.000100"}
        sc.reply({**thread, "text": "Make the last gate ask Ship it now?"}, sc.core)
        answer = slack.posted[1]
        assert answer["thread_ts"] == "1.000100" and answer["text"] == "Here it is."
        assert "Make the last gate ask Ship it now?" in script.prompts[0]
        (proposal,) = [b for b in answer["blocks"] if b["type"] == "actions"]
        assert answer["blocks"][1]["text"]["text"] == "*Edit ok*"
        assert run_id in answer["blocks"][-1]["elements"][0]["text"]  # the run page's link

        applied = sc.settle(pressed(answer, proposal["block_id"], "Apply"), sc.core)
        assert any(b.get("elements", [{}])[0].get("text") == "Applied by lee." for b in applied)
        (change,) = http.get(f"/api/runs/{run_id}/changes").json()
        assert change["by"] == "slack:lee" and change["payload"]["ask"] == "Ship it now?"
        cards = http.get(f"/api/runs/{run_id}/assistant").json()["messages"][1]["proposals"]
        assert [(c["state"], c["change_id"]) for c in cards] == [("applied", change["id"])]

        # A proposal the run moved past answers with the moved-on message, and nothing applies.
        sc.reply({**thread, "text": "Or ask Go?"}, sc.core)
        later = slack.posted[2]
        (other,) = [b for b in later["blocks"] if b["type"] == "actions"]
        (approve, _back) = buttons(gate)
        assert click(approve) is None
        until(lambda: http.get(f"/api/runs/{run_id}").json()["revision"] > run["revision"] + 1)
        with pytest.raises(sc.Refused) as refused:
            sc.settle(pressed(later, other["block_id"], "Apply"), sc.core)
        assert str(refused.value) == "The run moved on; open it to see the new proposal."
        assert len(http.get(f"/api/runs/{run_id}/changes").json()) == 1
