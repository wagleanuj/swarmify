import re
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
"""


def test_a_worker_runs_the_stage_and_the_page_shows_its_live_log(
    page: Page, base_url: str, data_dir: Path
) -> None:
    api = httpx.Client(base_url=base_url)
    world = api.post("/api/worlds", json={"name": "Video"}).json()
    try:
        wf = api.post(f"/api/worlds/{world['id']}/workflows", json={"yaml": RESEARCH}).json()
        run = api.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run['id']}")

        expect(page.get_by_test_id("status-line")).to_have_text(re.compile(r"^Done in "))
        log = page.get_by_test_id("log")
        expect(log).to_contain_text("Gilfoyle: Searching the world's memory")
        expect(log).to_contain_text("Gilfoyle: Claimed: the next task")
        expect(log).to_contain_text("Gilfoyle: Refused Bash: Bash is not allowed in this stage.")
        expect(log).to_contain_text("Gilfoyle: Writing notes/findings.md")
        expect(log).to_contain_text("Richard: Session ended")
        expect(log).to_contain_text("Erlich: Session ended")
        expect(page.get_by_test_id("agent").nth(1)).to_contain_text("worker · $0.0042")
        expect(page.get_by_test_id("agent").nth(1)).to_contain_text("Finished")
        # Planner, worker, judge and retro at $0.0042 each, about two cents.
        expect(page.get_by_test_id("budget")).to_contain_text("$0.02")
        expect(page.get_by_test_id("col-done")).to_contain_text(
            "Pull this week's top shortsdone by Gilfoyle"
        )
        expect(page.get_by_test_id("verdict")).to_contain_text("pass on loop 1 of 4.")
        expect(page.get_by_test_id("verdict")).to_contain_text("Two ideas, ranked.")
        cards = page.get_by_test_id("artifact-card")
        expect(cards.first).to_contain_text("#1 of 2")
        expect(cards.first).to_contain_text("Hook: Wait for it")
        expect(cards.nth(1)).to_contain_text("The chase")

        stage = run["stages"][0]["id"]
        folder = data_dir / "worlds" / world["id"] / "runs" / run["id"] / stage
        assert "Hooks in the first second win." in (folder / "notes/findings.md").read_text()
        assert (folder / "artifacts/ideas.md").read_text().endswith("1. Hooks")
        assert (folder / "artifacts/idea-hook-first.md").read_text().startswith('---\ntype: "idea"')
        assert "Two ideas, ranked." in (folder / "artifacts/verdict-1.md").read_text()
    finally:
        api.delete(f"/api/worlds/{world['id']}")
