"""The idea not picked, taken later: fork a done run from its pick gate and pick the other idea,
with lineage shown both ways; then from the library pin an idea, filter by type, and promote it
into a run that starts at the pick, its research skipped."""

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
        expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=20_000)
        run = page.url.rstrip("/").split("/")[-1]
        api.post(f"/runs/{run}/decision", json={"choice": "Hook first"}).raise_for_status()
        expect(status).to_have_text("Done. All 3 stages finished.", timeout=20_000)

        # Fork from the pick: research is copied, not run again, and the pick asks anew.
        page.get_by_role("button", name="Fork the run from pick").click()
        expect(page.get_by_test_id("forked-from")).to_contain_text(
            "Forked from Research then pick at stage 2. The earlier stages were copied"
        )
        expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=20_000)
        expect(page.get_by_test_id("artifact-card").first).to_contain_text("Hook first")
        fork = page.url.rstrip("/").split("/")[-1]
        assert fork != run
        api.post(f"/runs/{fork}/decision", json={"choice": "The chase"}).raise_for_status()
        expect(page.get_by_test_id("timeline")).to_contain_text("You picked The chase.")
        page.get_by_test_id("forked-from").get_by_role("link").click()
        expect(page.get_by_test_id("forks")).to_contain_text("Research then pick")
        expect(page.get_by_test_id("timeline")).to_contain_text("You picked Hook first.")
        page.get_by_role("link", name="runs").click()
        expect(page.get_by_test_id("fork-of")).to_contain_text("fork of Research then pick")

        # The library: every run's artifacts, filtered, pinned and promoted.
        page.goto(f"{base_url}/worlds/{world}/library")
        library = page.get_by_test_id("library")
        expect(library).to_contain_text("Hook first")
        count = page.get_by_test_id("count")
        page.get_by_label("Type").select_option("idea")
        expect(count).to_contain_text("4 of")  # two ideas in the run, two copied into the fork
        page.get_by_label("Run").select_option(index=1)
        expect(count).to_contain_text("2 of")
        page.get_by_role("button", name="Clear filters").click()

        idea = library.get_by_test_id("artifact").filter(has_text="Hook first").first
        idea.get_by_role("button", name="Pin Hook first").click()
        expect(idea).to_have_attribute("data-pinned", "true")
        expect(idea).to_contain_text("pinned")
        page.reload()
        expect(library.locator("[data-pinned=true]")).to_contain_text("Hook first")

        idea = library.locator("[data-pinned=true]")
        idea.get_by_role("button", name="Promote Hook first").click()
        form = idea.get_by_role("form", name="Promote Hook first")
        form.get_by_label("Start at").select_option(label="1. pick")
        form.get_by_role("button", name="Start run").click()
        expect(page.get_by_test_id("promoted-from")).to_contain_text("idea-hook-first.md")
        expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=20_000)
        expect(page.get_by_test_id("timeline").locator("[data-status=skipped]")).to_contain_text(
            "research"
        )
    finally:
        api.delete(f"/worlds/{world}")
