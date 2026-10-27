"""A run survives the app restarting (story 37): it waits at its gate across a stop and a
start on the same data folder, and finishes without running its done stage again."""

from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

FLOW = """name: Research then pick
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
  - gate: pick
    ask: Which idea do we shoot?
    kind: pick
"""


def test_a_run_waiting_at_a_gate_goes_on_after_a_restart(
    page: Page, serve: Any, tmp_path: Path
) -> None:
    with serve(tmp_path) as base_url:
        api = httpx.Client(base_url=f"{base_url}/api")
        world = api.post("/worlds", json={"name": "Video"}).json()["id"]
        wf = api.post(f"/worlds/{world}/workflows", json={"yaml": FLOW}).json()["workflow_id"]
        run = api.post(f"/workflows/{wf}/runs", json={}).json()["id"]
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=20_000)
        page.goto("about:blank")  # let go of the event stream before the server stops

    with serve(tmp_path) as base_url:
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        expect(status).to_have_text("Waiting for you at stage 2: pick.")
        expect(page.get_by_test_id("timeline").locator("[data-status=done]")).to_contain_text(
            "research"
        )
        page.goto(f"{base_url}/gates")
        card = page.get_by_test_id(f"gate-{run}")
        card.get_by_label("Your pick").fill("Hook first")
        card.get_by_role("button", name="Continue with this pick").click()
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        expect(status).to_have_text("Done. All 3 stages finished.", timeout=20_000)
        # Planner, worker, judge and retro once each: research did not run again.
        expect(page.get_by_test_id("run-cost")).to_contain_text("$0.0168")
        page.goto("about:blank")
