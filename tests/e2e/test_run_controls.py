"""Acting on runs from their pages: pause and resume, the per-world cap that queues a run, abort
reaching a child run, a Hooli baseline measured beside the swarm, and an ad hoc run from a typed
goal saved as a workflow. Every change reaches the open page over the world's event stream."""

from typing import Any

import httpx
from playwright.sync_api import Page, expect

BRIEF = """name: Brief
stages:
  - gate: brief
    ask: Go?
"""
RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
"""


def test_pause_resume_the_cap_and_abort_reaching_the_child(
    page: Page, base_url: str, until: Any
) -> None:
    api = httpx.Client(base_url=f"{base_url}/api")
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    try:
        brief = api.post(f"/worlds/{world}/workflows", json={"yaml": BRIEF}).json()["workflow_id"]
        parent = f"name: Parent\nstages:\n  - workflow: child\n    runs: {brief}\n"
        wf = api.post(f"/worlds/{world}/workflows", json={"yaml": parent}).json()["workflow_id"]

        # A workflow stage is a child run, linked both ways (story 36).
        run = api.post(f"/workflows/{wf}/runs", json={}).json()["id"]
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text(
            "Stage 1 of 2, child, waits on its child run Brief.", timeout=20_000
        )
        page.get_by_test_id("child-run").get_by_role("link", name="Brief").click()
        expect(page.get_by_test_id("child-of")).to_contain_text("child of Parent")
        expect(status).to_have_text("Waiting for you at stage 1: brief.")
        child = page.url.rstrip("/").split("/")[-1]

        # Pause at the gate, then resume (story 31).
        page.get_by_role("button", name="Pause").click()
        expect(status).to_have_text("Paused. Nothing moves until it is resumed.")
        page.get_by_role("button", name="Resume").click()
        expect(status).to_have_text("Waiting for you at stage 1: brief.")

        # Aborting the parent aborts the child (story 32).
        page.get_by_test_id("child-of").get_by_role("link").click()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Abort").click()
        expect(status).to_have_text("Aborted. Finished stages keep their artifacts.")
        until(lambda: api.get(f"/runs/{child}").json()["status"] == "aborted")

        # One run at a time: the second waits in the queue until the first ends (story 26).
        api.patch(f"/worlds/{world}", json={"run_cap": 1}).raise_for_status()
        first = api.post(f"/workflows/{brief}/runs", json={}).json()["id"]
        until(lambda: api.get(f"/runs/{first}").json()["status"] == "waiting_gate")
        second = api.post(f"/workflows/{brief}/runs", json={}).json()["id"]
        page.goto(f"{base_url}/worlds/{world}/runs")
        runs = page.get_by_test_id("run-list")
        expect(runs.locator("li", has_text="queued")).to_have_count(1)
        expect(runs.locator("li", has_text="needs you")).to_have_count(1)
        api.post(f"/runs/{first}/abort").raise_for_status()
        expect(runs.locator("li", has_text="queued")).to_have_count(0, timeout=20_000)
        expect(runs.locator("li", has_text="needs you")).to_have_count(1)
        assert api.get(f"/runs/{second}").json()["status"] == "waiting_gate"
        api.post(f"/runs/{second}/abort").raise_for_status()
    finally:
        api.delete(f"/worlds/{world}")


def test_a_baseline_beside_the_swarm_and_an_ad_hoc_run_saved_as_a_workflow(
    page: Page, base_url: str
) -> None:
    api = httpx.Client(base_url=f"{base_url}/api")
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    try:
        api.post(f"/worlds/{world}/workflows", json={"yaml": RESEARCH}).raise_for_status()
        page.goto(f"{base_url}/worlds/{world}")
        page.get_by_role("button", name="Start a Hooli baseline of Research").click()
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Done. All 1 stages finished.", timeout=20_000)
        expect(page.get_by_test_id("baseline")).to_contain_text("This is a Hooli baseline")

        page.goto(f"{base_url}/worlds/{world}")
        page.get_by_role("button", name="Start a run of Research").click()
        expect(status).to_have_text("Done. All 2 stages finished.", timeout=20_000)
        expect(page.get_by_test_id("baseline")).to_contain_text("Hooli ran this alone for")
        page.get_by_role("link", name="runs").click()
        expect(page.get_by_test_id("run-list")).to_contain_text("Hooli baseline")

        # An ad hoc run from a typed goal (story 20), then saved as a workflow (story 21).
        page.goto(f"{base_url}/worlds/{world}")
        page.get_by_role("textbox", name="Goal").fill("Find five hooks for next week's short.")
        page.get_by_role("button", name="Run this goal").click()
        expect(status).to_have_text("Done. All 2 stages finished.", timeout=20_000)
        expect(page.get_by_role("heading", level=1)).to_contain_text("ad hoc")
        page.get_by_role("button", name="Save as workflow").click()
        expect(page.get_by_test_id("versions")).to_contain_text("v1")
        expect(page.get_by_label("Workflow YAML")).to_contain_text("Find five hooks")
    finally:
        api.delete(f"/worlds/{world}")
