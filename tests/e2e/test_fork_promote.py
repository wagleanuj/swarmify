"""The idea not picked, taken later: fork a done run from its pick gate and pick the other idea,
with lineage shown both ways; then from the library pin an idea, filter by type, and promote it
into a run that starts at the pick, its research skipped."""

import re

import httpx
from playwright.sync_api import Page, expect

FLOW = """name: Research then pick
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
  - gate: pick
    ask: Which idea do we shoot?
    kind: pick
"""


def test_fork_from_the_pick_then_pin_filter_and_promote_from_the_library(
    page: Page, base_url: str
) -> None:
    api = httpx.Client(base_url=f"{base_url}/api")
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    try:
        api.post(f"/worlds/{world}/workflows", json={"yaml": FLOW}).raise_for_status()
        page.goto(f"{base_url}/worlds/{world}")
        page.get_by_role("button", name="Start a run of Research then pick").click()
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Waiting for you: Which idea do we shoot?", timeout=20_000)
        run = page.url.split("?")[0].rstrip("/").split("/")[-1]
        api.post(f"/runs/{run}/decision", json={"choice": "Hook first"}).raise_for_status()
        expect(status).to_have_text(re.compile(r"^Done in "), timeout=20_000)

        # Fork from the pick: research is copied, not run again, and the pick asks anew.
        pick = re.compile(r"^Step 2, pick,")
        page.get_by_test_id("timeline").get_by_role("button", name=pick).click()
        page.get_by_role("button", name="Run actions").click()
        page.get_by_role("menuitem", name=re.compile(r"^Fork from pick")).click()
        expect(page.get_by_test_id("forked-from")).to_contain_text(
            "Forked from Research then pick at stage 2. The earlier stages were copied"
        )
        expect(status).to_have_text("Waiting for you: Which idea do we shoot?", timeout=20_000)
        fork = page.url.split("?")[0].rstrip("/").split("/")[-1]
        assert fork != run
        # The pick it waits at leads its panel, with the copied ideas to choose from.
        page.get_by_test_id("timeline").get_by_role("button", name=pick).click()
        expect(
            page.get_by_test_id(f"gate-{fork}").get_by_test_id("pick-card").first
        ).to_contain_text("Hook first")
        api.post(f"/runs/{fork}/decision", json={"choice": "The chase"}).raise_for_status()
        page.get_by_test_id("timeline").get_by_role("button", name=pick).click()
        expect(page.get_by_test_id("decision")).to_have_text('You picked "The chase".')
        page.get_by_test_id("forked-from").get_by_role("link").click()
        expect(page.get_by_test_id("forks")).to_contain_text("Research then pick")
        page.get_by_test_id("timeline").get_by_role("button", name=pick).click()
        expect(page.get_by_test_id("decision")).to_have_text('You picked "Hook first".')
        page.get_by_role("navigation", name="Main").get_by_role("link", name="Runs").click()
        expect(page.get_by_test_id("fork-of")).to_contain_text("fork of Research then pick")

        # The library: every run's artifacts, filtered, pinned and promoted.
        page.goto(f"{base_url}/worlds/{world}/library")
        library = page.get_by_test_id("library")
        expect(library).to_contain_text("Hook first")
        count = page.get_by_test_id("count")
        page.get_by_role("navigation", name="Types").get_by_role(
            "link", name=re.compile("^idea")
        ).click()
        expect(count).to_contain_text("4 of")  # two ideas in the run, two copied into the fork
        page.get_by_label("Run").select_option(index=1)
        expect(count).to_contain_text("2 of")
        page.get_by_role("button", name="Clear filters").click()

        library.get_by_test_id("artifact").filter(has_text="Hook first").first.click()
        preview = page.get_by_test_id("preview")
        preview.get_by_role("button", name="Pin Hook first").click()
        expect(preview.get_by_role("status")).to_have_text("Pinned. Later stages read it.")
        expect(library.locator("[data-pinned=true]")).to_contain_text("Pinned · Hook first")
        page.reload()
        expect(library.locator("[data-pinned=true]")).to_contain_text("Hook first")

        # The address kept the item, so the preview is still there to start a run from.
        preview.get_by_role("button", name="Start a run from here").click()
        page.get_by_role("menuitem", name=re.compile("^pick")).first.click()
        expect(page.get_by_test_id("promoted-from")).to_contain_text("idea-hook-first.md")
        expect(status).to_have_text("Waiting for you: Which idea do we shoot?", timeout=20_000)
        expect(page.get_by_test_id("timeline").locator("[data-status=skipped]")).to_contain_text(
            "research"
        )
    finally:
        api.delete(f"/worlds/{world}")
