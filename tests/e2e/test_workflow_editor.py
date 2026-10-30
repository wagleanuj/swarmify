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
    # A new workflow opens on the first starter, with the others a click away.
    starters = page.get_by_role("region", name="Starter workflows").get_by_role("button")
    expect(starters).to_have_count(4)
    for name in ("Research brief", "Draft with approval", "Scheduled monitor", "Weekly short"):
        expect(starters.filter(has_text=name)).to_have_count(1)
    expect(page.get_by_role("heading", name="Research brief", level=1)).to_be_visible()
    expect(page.get_by_test_id("problems")).to_contain_text("Checks pass")
    starters.filter(has_text="Scheduled monitor").click()
    expect(page.get_by_test_id("schedule")).to_contain_text("0 8 * * 1")
    expect(page.get_by_test_id("problems")).to_contain_text("Checks pass")
    starters.filter(has_text="Weekly short").click()
    expect(page.get_by_role("heading", name="Weekly short", level=1)).to_be_visible()
    expect(page.get_by_test_id("problems")).to_contain_text("Checks pass")
    expect(page.get_by_test_id("stage-rows")).to_contain_text("research")

    # A publish connector before the gate, fed an artifact nobody made: both rules fire inline.
    page.get_by_role("tab", name="YAML").click()
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
    expect(problems).to_contain_text("Checks pass")
    page.get_by_role("button", name="Save as version 1").click()
    expect(page.get_by_test_id("versions")).to_contain_text("v1")
    expect(page).to_have_url(re.compile(r"/workflows/wf_"))

    editor.fill(starter.replace("Weekly short", "Daily short"))
    expect(page.get_by_role("heading", name="Daily short v1", level=1)).to_be_visible()
    page.get_by_role("button", name="Save as version 2").click()
    expect(page.get_by_test_id("check-status")).to_have_text(
        "Saved as version 2; runs already started keep their version."
    )
    expect(page.get_by_test_id("versions")).to_contain_text("v2")
    page.get_by_role("button", name="Open v1").click()
    expect(editor).to_have_value(starter)

    page.get_by_role("navigation", name="Main").get_by_role("link", name="Overview").click()
    expect(page.get_by_test_id("workflow-list")).to_contain_text("Daily short")
    expect(page.get_by_test_id("workflow-list")).to_contain_text("v2")
