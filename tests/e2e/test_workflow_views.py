"""The editor's stage list and YAML as two views of one text, schedule and defaults, the cycle
and depth rules, a run keeping the version it started with, and a copy into another world."""

import re

import httpx
from playwright.sync_api import Page, expect

LEAF = "name: Leaf\nstages:\n  - gate: ok\n    ask: Fine?\n"


def calls(runs: str, name: str = "Nest") -> str:
    return f"name: {name}\nstages:\n  - workflow: child\n    runs: {runs}\n"


def test_stage_list_and_yaml_stay_in_step_and_every_rule_shows(page: Page, base_url: str) -> None:
    api = httpx.Client(base_url=f"{base_url}/api")
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    other = api.post("/worlds", json={"name": "Research"}).json()["id"]
    try:
        page.goto(f"{base_url}/worlds/{world}/workflows/new")
        cards = page.get_by_test_id("stage-rows")
        expect(cards.locator("li")).to_have_count(2)
        # Every swarm stage declares a goal, a done condition, tools and an output type (14).
        expect(page.get_by_label("Done when of research")).to_have_value(
            "5 ideas ranked, each with a hook line and a why"
        )
        expect(page.get_by_label("Output type of research")).to_have_value("list[idea]")
        cards.get_by_role("button", name=re.compile("pick")).click()
        page.get_by_label("Ask of pick").fill("Which one?")
        page.get_by_label("Kind of pick").select_option("approve")  # story 16
        page.get_by_label("Schedule (cron, UTC)").fill("0 8 * * 1")
        expect(page.get_by_test_id("next-run")).to_contain_text("Next run")  # story 18
        page.get_by_label("Budget (USD)").fill("3")
        page.get_by_label("Agents").fill("2")  # story 19
        page.get_by_label("Stage type to add").select_option("connector")
        page.get_by_role("button", name="Add stage").click()
        expect(cards.locator("li")).to_have_count(3)
        page.get_by_label("Output of render").fill("short.mp4")  # story 15
        expect(cards).to_contain_text("render")

        page.get_by_role("tab", name="YAML").click()
        yaml = page.get_by_label("Workflow YAML")
        for line in ("schedule: 0 8 * * 1", "budget_usd: 3", "agents: 2", "ask: Which one?"):
            expect(yaml).to_contain_text(line)
        expect(yaml).to_contain_text("kind: approve")
        expect(yaml).to_contain_text("connector: render")
        # A render stage names its Remotion project and composition before it can be saved.
        expect(page.get_by_test_id("problems")).to_contain_text("needs params.project")
        yaml.fill(
            re.sub(
                r"( *)calls: remotion_render\n",
                r"\g<0>\1params: {project: /tmp, composition: Short}\n",
                yaml.input_value(),
            )
        )
        expect(page.get_by_test_id("problems")).to_contain_text("Checks pass")
        page.get_by_role("button", name="Save as version 1").click()
        expect(page.get_by_test_id("schedule")).to_contain_text("It starts on its own")

        # A run keeps the version it started with while the workflow moves on (story 11).
        workflow = page.url.split("?")[0].rstrip("/").split("/")[-1]
        run = api.post(f"/workflows/{workflow}/runs", json={}).json()["id"]
        yaml.fill(yaml.input_value().replace("Weekly short", "Weekly short two"))
        page.get_by_role("button", name="Save as version 2").click()
        expect(page.get_by_test_id("versions")).to_contain_text("v2")
        page.goto(f"{base_url}/worlds/{world}/runs/{run}")
        expect(page.get_by_role("heading", level=1)).to_contain_text("v1")
        api.post(f"/runs/{run}/abort").raise_for_status()

        # Workflows that would run themselves, or nest deeper than two, are refused (story 13).
        leaf = api.post(f"/worlds/{world}/workflows", json={"yaml": LEAF}).json()["workflow_id"]
        mid = api.post(f"/worlds/{world}/workflows", json={"yaml": calls(leaf)}).json()
        top = api.post(f"/worlds/{world}/workflows", json={"yaml": calls(mid["workflow_id"])})
        page.goto(f"{base_url}/worlds/{world}/workflows/{leaf}?tab=yaml")
        yaml = page.get_by_label("Workflow YAML")
        yaml.fill(calls(top.json()["workflow_id"], "Leaf"))
        problems = page.get_by_test_id("problems")
        expect(problems).to_contain_text("this workflow would run itself")
        expect(page.get_by_role("button", name="Save as version 2")).to_be_disabled()
        page.goto(f"{base_url}/worlds/{world}/workflows/new?tab=yaml")
        yaml.fill(calls(top.json()["workflow_id"]))
        expect(problems).to_contain_text("workflows nest 3 deep, the limit is 2")

        # Copy a workflow into another world, staying here (story 22).
        page.goto(f"{base_url}/worlds/{world}/workflows/{leaf}")
        page.get_by_role("button", name="Copy to another world").click()
        page.get_by_role("menuitem", name=re.compile("Research")).click()
        expect(page.get_by_test_id("copied")).to_contain_text("Copied to Research")
        expect(page).to_have_url(re.compile(f"/worlds/{world}/workflows/{leaf}"))
        page.get_by_role("link", name="Open the copy").click()
        expect(page).to_have_url(re.compile(f"/worlds/{other}/workflows/wf_"))
        expect(page.get_by_test_id("versions")).to_contain_text("v1")
    finally:
        api.delete(f"/worlds/{world}")
        api.delete(f"/worlds/{other}")
