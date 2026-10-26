from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
"""


def test_a_run_s_notes_rebuild_the_graph_live_and_the_page_can_query_it(
    page: Page, base_url: str, data_dir: Path
) -> None:
    api = httpx.Client(base_url=base_url)
    world = api.post("/api/worlds", json={"name": "Video"}).json()
    try:
        page.goto(f"{base_url}/worlds/{world['id']}/graph")
        expect(page.get_by_test_id("rebuilt")).to_have_text("Not built yet")
        expect(page.get_by_test_id("graph-empty")).to_be_visible()

        wf = api.post(f"/api/worlds/{world['id']}/workflows", json={"yaml": RESEARCH}).json()
        api.post(f"/api/workflows/{wf['workflow_id']}/runs", json={})

        rebuilt = page.get_by_test_id("rebuilt")
        expect(rebuilt).to_contain_text(
            "Anton rebuilt it just now: +2 nodes, +1 edges", timeout=20000
        )
        expect(page.get_by_test_id("graph-size")).to_contain_text("2 nodes")
        expect(page.get_by_test_id("god-nodes")).to_contain_text("Short hooks")

        page.get_by_label("Ask the graph").fill("what beats the retention cliff?")
        page.get_by_role("button", name="Ask", exact=True).click()
        expect(page.get_by_test_id("answer")).to_contain_text("Short hooks", timeout=20000)
        assert (data_dir / "worlds" / world["id"] / "graphify-out/graph.json").exists()
    finally:
        api.delete(f"/api/worlds/{world['id']}")
