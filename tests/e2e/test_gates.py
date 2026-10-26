import shlex
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

ECHO = Path(__file__).parents[2] / "packages/connectors/tests/echo_server.py"
ECHO_CONNECTOR = {
    "name": "echo",
    "kind": "stage",
    "server": shlex.join([sys.executable, str(ECHO)]),
    "stage_types": ["connector"],
}
REVIEW = """name: Review loop
stages:
  - connector: render
    calls: echo
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
"""


def test_answer_a_gate_from_the_inbox_and_watch_the_run_finish(page: Page, base_url: str) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Video"}).json()
    try:
        connectors = f"{base_url}/api/worlds/{world['id']}/connectors"
        httpx.post(connectors, json=ECHO_CONNECTOR).raise_for_status()
        wf = httpx.post(f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": REVIEW})
        run = httpx.post(f"{base_url}/api/workflows/{wf.json()['workflow_id']}/runs", json={})
        page.goto(f"{base_url}/gates")
        card = page.get_by_test_id(f"gate-{run.json()['id']}")
        expect(card).to_contain_text("Watch it. Approve or send back.", timeout=15_000)
        expect(card).to_contain_text("2 of 2 send-backs left")

        card.get_by_role("button", name="Send back...").click()
        card.get_by_label(r"Note").fill("Darker.")
        card.get_by_role("button", name="Send back", exact=True).click()
        expect(card).to_contain_text("1 of 2 send-backs left", timeout=15_000)

        card.get_by_role("button", name="Approve").click()
        empty = page.get_by_test_id("no-gates")
        expect(empty).to_contain_text("Nothing is waiting on you", timeout=15_000)

        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run.json()['id']}")
        expect(page.get_by_test_id("status-line")).to_have_text(
            "Done. All 2 stages finished. Sent back 1 of 2 times.", timeout=15_000
        )
        expect(page.get_by_test_id("timeline")).to_contain_text("Sent back with: Darker.")
        expect(page.get_by_test_id("timeline")).to_contain_text("You approved.")
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
