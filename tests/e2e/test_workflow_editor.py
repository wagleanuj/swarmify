import re

import httpx
from playwright.sync_api import Page, expect

PUBLISH = "  - connector: youtube\n    input: short.mp4\n    params: {title: Hooks}\n"
GATE = "  - gate: pick\n"


def test_write_check_and_version_a_workflow(page: Page, base_url: str) -> None:
    try:
        _run(page, base_url)
    finally:
        # The server and its data dir are shared by the session; leave the worlds page empty.
        for world in httpx.get(f"{base_url}/api/worlds").json():
            httpx.delete(f"{base_url}/api/worlds/{world['id']}")


def _run(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.get_by_label("Name").fill("Video")
    page.get_by_role("button", name="Create world").click()
    expect(page.get_by_test_id("no-workflows")).to_be_visible()

    page.get_by_role("link", name="New workflow").click()
    expect(page.get_by_role("heading", name="Weekly short", level=1)).to_be_visible()
    expect(page.get_by_test_id("problems")).to_contain_text("No problems")
    expect(page.get_by_test_id("stage-rail")).to_contain_text("research")

    # A publish connector before the gate, fed an artifact nobody made: both rules fire inline.
    editor = page.get_by_label("Workflow YAML")
    starter = editor.input_value()
    assert GATE in starter
    editor.fill(starter.replace(GATE, PUBLISH + GATE))
    problems = page.get_by_test_id("problems")
    expect(problems).to_contain_text("2 problems to fix before saving")
    expect(problems).to_contain_text(
        "youtube: publish connector 'youtube' must come right after a gate"
    )
    expect(problems).to_contain_text("input 'short.mp4' is not produced by an earlier stage")
    expect(page.get_by_role("button", name="Save as version 1")).to_be_disabled()

    # Moved after the gate, only the missing input remains.
    editor.fill(starter + PUBLISH)
    expect(problems).to_contain_text("1 problem to fix before saving")
    expect(problems).not_to_contain_text("must come right after a gate")

    editor.fill(starter)
    expect(problems).to_contain_text("No problems")
    page.get_by_role("button", name="Save as version 1").click()
    expect(page.get_by_test_id("versions")).to_contain_text("v1")
    expect(page).to_have_url(re.compile(r"/workflows/wf_"))

    editor.fill(starter.replace("Weekly short", "Daily short"))
    expect(page.get_by_role("heading", name="Daily short", level=1)).to_be_visible()
    page.get_by_role("button", name="Save as version 2").click()
    expect(page.get_by_test_id("check-status")).to_have_text("Saved as version 2.")
    expect(page.get_by_test_id("versions")).to_contain_text("v2")
    page.get_by_role("button", name="Open v1").click()
    expect(editor).to_have_value(starter)

    page.get_by_role("link", name="video").click()
    expect(page.get_by_test_id("workflow-list")).to_contain_text("Daily short")
    expect(page.get_by_test_id("workflow-list")).to_contain_text("v2")
    expect(page.get_by_test_id("activity")).to_contain_text("Workflow saved")
