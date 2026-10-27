"""Russ, the Budget, as the operator meets it: the monthly ceiling and the Slack hook on the
settings page, a run started with a tiny budget that warns past 80 percent, stops at 100 at a
budget gate offering add money or abort, and Slack hearing of each with a deep link.

Each scripted session costs $0.0042, so a $0.005 run warns after the planner and is stopped
before the judge."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
"""


@pytest.fixture
def slack() -> Iterator[tuple[str, list[str]]]:
    """A stand-in Slack incoming webhook on localhost: its URL and the texts posted to it."""
    texts: list[str] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            texts.append(json.loads(body)["text"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", texts
    server.shutdown()


def test_a_run_warns_at_80_percent_and_waits_at_100_for_money_or_an_abort(
    page: Page, serve: Any, tmp_path: Path, slack: tuple[str, list[str]]
) -> None:
    hook, texts = slack
    with serve(tmp_path) as base_url:
        api = httpx.Client(base_url=f"{base_url}/api")
        world = api.post("/worlds", json={"name": "Video"}).json()["id"]
        workflow = api.post(f"/worlds/{world}/workflows", json={"yaml": RESEARCH}).json()

        page.goto(f"{base_url}/worlds/{world}/settings")
        page.get_by_label("Monthly budget, dollars").fill("50")
        page.get_by_role("button", name="Save settings").click()
        expect(page.get_by_text("Settings saved.")).to_be_visible()
        page.get_by_label("Incoming webhook URL").fill(hook)
        page.get_by_role("button", name="Save webhook").click()
        expect(page.get_by_test_id("slack-state")).to_contain_text("Posting to http://127.0.0.1")
        page.get_by_role("button", name="Send a test message").click()
        expect(page.get_by_text("Test sent.")).to_be_visible()
        assert texts and "Swarmify is connected" in texts[-1]

        # A test run with its budget overridden at the start (story 23).
        start = f"/workflows/{workflow['workflow_id']}/runs"
        run = api.post(start, json={"budget_usd": 0.005}).json()["id"]
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text(
            "Out of budget at stage 1: research. Add money or abort.", timeout=20_000
        )
        expect(page.get_by_test_id("budget-line")).to_contain_text("Russ stopped every agent")
        page.get_by_role("link", name="Add money or abort").click()
        card = page.get_by_test_id(f"gate-{run}")
        expect(card).to_have_attribute("data-kind", "budget")
        expect(card).to_contain_text("Russ has spent")
        card.get_by_role("button", name="Add money and go on").click()
        expect(page.get_by_test_id("no-gates")).to_be_visible(timeout=20_000)

        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        expect(status).to_have_text("Done. All 2 stages finished.", timeout=20_000)
        expect(page.get_by_test_id("budget-line")).to_contain_text("Past 80 percent")

        # A second one is stopped the same way and aborted from the gate.
        second = api.post(start, json={"budget_usd": 0.005}).json()["id"]
        page.goto(f"{base_url}/gates")
        card = page.get_by_test_id(f"gate-{second}")
        expect(card).to_contain_text("Russ has spent", timeout=20_000)
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="Abort the run").click()
        expect(page.get_by_test_id("no-gates")).to_be_visible(timeout=20_000)
        assert api.get(f"/runs/{second}").json()["status"] == "aborted"

        page.goto(f"{base_url}/worlds/{world}/settings")
        expect(page.get_by_test_id("spent")).to_contain_text("an estimate of $50.00")

        link = f"/worlds/{world}/runs/{run}"
        mine = [t for t in texts if link in t or "/gates" in t]
        assert any("It pauses at 100 percent" in t and link in t for t in mine), texts
        assert any("its budget is spent" in t and "/gates|Add money or abort" in t for t in mine)
        assert any(t.startswith("*Research* ") and link in t and "done" in t for t in mine), texts
