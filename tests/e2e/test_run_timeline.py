import httpx
from playwright.sync_api import Page, expect

SHORT = """name: Render then pick
stages:
  - connector: render
    calls: remotion_render
  - gate: pick
    ask: Ship it?
"""


def test_start_a_run_and_watch_it_reach_its_gate_live(page: Page, base_url: str) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Video"}).json()
    try:
        httpx.post(f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": SHORT})
        page.goto(f"{base_url}/worlds/{world['id']}")
        page.get_by_role("button", name="Start a run of Render then pick").click()

        # The page loads the queued run, then the world's event stream moves it along.
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=15_000)
        timeline = page.get_by_test_id("timeline")
        expect(timeline.locator("[data-status=done]")).to_contain_text("render")
        expect(timeline.locator("[data-status=waiting]")).to_contain_text("pick")

        page.get_by_role("link", name="runs").click()
        expect(page.get_by_test_id("run-list")).to_contain_text("needs you")
        expect(page.get_by_test_id("run-list")).to_contain_text("1 of 2 stages")
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
