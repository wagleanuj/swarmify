import shlex
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

ECHO = Path(__file__).parents[2] / "packages/connectors/tests/echo_server.py"
SHORT = """name: Render then pick
stages:
  - connector: render
    calls: echo
  - gate: pick
    ask: Ship it?
"""


def test_start_a_run_and_watch_it_reach_its_gate_live(page: Page, base_url: str) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Video"}).json()
    echo = {
        "name": "echo",
        "kind": "stage",
        "server": shlex.join([sys.executable, str(ECHO)]),
        "stage_types": ["connector"],
    }
    try:
        httpx.post(f"{base_url}/api/worlds/{world['id']}/connectors", json=echo).raise_for_status()
        httpx.post(f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": SHORT})
        page.goto(f"{base_url}/worlds/{world['id']}")
        page.get_by_role("button", name="Start a run of Render then pick").click()

        # The page loads the queued run, then the world's event stream moves it along.
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Waiting for you: Ship it?", timeout=15_000)
        timeline = page.get_by_test_id("timeline")
        expect(timeline.locator("[data-status=done]")).to_contain_text("render")
        expect(timeline.locator("[data-status=waiting]")).to_contain_text("pick")

        page.get_by_role("navigation", name="Main").get_by_role("link", name="Runs").click()
        expect(page.get_by_test_id("run-list")).to_contain_text("needs you")
        # The row's time strip has a segment per stage.
        expect(page.get_by_test_id("run-list").get_by_role("img")).to_have_count(3)
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
