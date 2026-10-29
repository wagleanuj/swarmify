import re
import time

import httpx
from playwright.sync_api import Page, expect

GATE_THEN_SWARM = """name: Brief then research
stages:
  - gate: brief
    ask: Go?
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    tools: [web_search]
"""


def test_add_a_task_by_hand_and_watch_it_move_across_the_columns(page: Page, base_url: str) -> None:
    api = httpx.Client(base_url=base_url)
    world = api.post("/api/worlds", json={"name": "Video"}).json()
    try:
        wf = api.post(f"/api/worlds/{world['id']}/workflows", json={"yaml": GATE_THEN_SWARM})
        run = api.post(f"/api/workflows/{wf.json()['workflow_id']}/runs", json={}).json()
        for _ in range(100):
            if api.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate":
                break
            time.sleep(0.1)
        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run['id']}")

        page.get_by_test_id("timeline").get_by_role(
            "button", name=re.compile(r"^Step 2, research,")
        ).click()
        expect(page.get_by_test_id("stage-panel").get_by_role("heading", level=2)).to_have_text(
            "research"
        )
        page.get_by_role("textbox", name="New task").fill("Check @datingdecoded this week")
        page.get_by_role("button", name="Add task").click()
        expect(page.get_by_test_id("col-open")).to_contain_text("Check @datingdecoded this week")
        expect(page.get_by_test_id("col-open")).to_contain_text("added by you")

        # An agent claims it over HTTP; the stream moves the card without a reload.
        tasks = f"/api/runs/{run['id']}/stages/{run['stages'][1]['id']}/tasks"
        task_id = api.get(tasks).json()[0]["id"]
        api.post(f"/api/tasks/{task_id}/claim", json={"agent": "gilfoyle"}).raise_for_status()
        expect(page.get_by_test_id("col-claimed")).to_contain_text("claimed by gilfoyle")
        api.post(f"/api/tasks/{task_id}/finish", json={"agent": "gilfoyle", "note": "Done."})
        expect(page.get_by_test_id("col-done")).to_contain_text("gilfoyle: Done.")
        expect(page.get_by_test_id("whiteboard-summary")).to_have_text("0 open, 0 claimed, 1 done.")
    finally:
        api.delete(f"/api/worlds/{world['id']}")
