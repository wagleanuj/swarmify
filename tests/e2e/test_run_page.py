"""The run page (issue 130) through a scripted run with a speech stage and a build stage: the
status line, the result in place, a panel per step, and the gate answered on the page."""

import re

import httpx
from playwright.sync_api import Page, expect

VOICED = """name: Voice then build
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
    produces: [ideas.md]
  - connector: voice
    calls: speech
    input: ideas.md
    output: timings.json
  - connector: video
    calls: remotion_build
    inputs: [ideas.md, timings.json]
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
"""


def test_a_voiced_build_shows_its_takes_its_build_and_its_video_then_the_gate_is_answered_here(
    page: Page, base_url: str
) -> None:
    api = httpx.Client(base_url=base_url)
    world = api.post("/api/worlds", json={"name": "Shorts"}).json()
    try:
        wf = api.post(f"/api/worlds/{world['id']}/workflows", json={"yaml": VOICED})
        assert wf.status_code == 201, wf.text
        run = api.post(f"/api/workflows/{wf.json()['workflow_id']}/runs", json={}).json()
        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run['id']}")

        status = page.get_by_test_id("status-line")
        expect(status).to_have_text(
            "Waiting for you: Watch it. Approve or send back.", timeout=20_000
        )
        expect(page.locator(".crumb")).to_have_text("worlds / shorts / runs")
        expect(page.get_by_test_id("result").get_by_test_id("player")).to_be_visible()
        expect(page.get_by_test_id("budget")).to_have_text(
            re.compile(r"^\$0\.\d\d of \$5\.00 budget$")
        )
        rail = page.get_by_test_id("timeline")
        expect(rail.locator("[data-status=done]")).to_have_count(3)
        expect(rail.locator("[data-status=waiting]")).to_contain_text("waiting")

        rail.get_by_role("button", name=re.compile("^Step 2, voice")).click()
        expect(page).to_have_url(re.compile(r"\?stage=stage_"))
        speech = page.get_by_test_id("speech")
        expect(speech).to_contain_text("2 scenes · 3.5 s of narration")
        expect(speech.get_by_test_id("take").first).to_contain_text("hook")
        expect(speech).to_contain_text("Line 1 runs into line 2 with no silence to cut at.")

        rail.get_by_role("button", name=re.compile("^Step 3, video")).click()
        build = page.get_by_test_id("build-log")
        expect(build).to_contain_text("Installed packages")
        expect(build).to_contain_text("Wrote src/Main.tsx")
        expect(page.get_by_test_id("render-progress")).to_contain_text("Rendered every frame")
        expect(page.get_by_test_id("project")).to_contain_text("/build")
        page.get_by_text("Technical details").click()
        expect(page.get_by_test_id("call-request")).to_contain_text('"composition": "Main"')

        # A link to a step opens the same step.
        page.reload()
        expect(page.get_by_test_id("build-log")).to_contain_text("Installed packages")

        page.get_by_role("button", name="Answer now").click()
        page.get_by_role("button", name="Approve").click()
        expect(status).to_have_text(re.compile(r"^Done in \d+ s\.$"), timeout=20_000)
        rail.get_by_role("button", name=re.compile("^Step 4, review")).click()
        expect(page.get_by_test_id("decision")).to_have_text('You approved "video.mp4".')
    finally:
        api.delete(f"/api/worlds/{world['id']}")
