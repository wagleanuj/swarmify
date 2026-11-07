"""The workflow editor on a phone: a workflow opened from its card, a stage edited in its sheet
and saved from the pinned bar, a new workflow made from a starter, and no tab wider than the
screen (Mobile UX Spec, T3)."""

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

PHONE: ViewportSize = {"width": 390, "height": 844}
FLOW = """name: Phone flow
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
  - gate: pick
    ask: Which idea do we shoot?
    kind: pick
"""


@pytest.fixture
def world(base_url: str) -> Iterator[tuple[str, str]]:
    api = httpx.Client(base_url=f"{base_url}/api", timeout=30)
    world = api.post("/worlds", json={"name": "Phone"}).json()["id"]
    made = api.post(f"/worlds/{world}/workflows", json={"yaml": FLOW})
    assert made.status_code == 201, made.text
    yield world, made.json()["workflow_id"]
    api.delete(f"/worlds/{world}")


@pytest.fixture
def phone(browser: Browser, base_url: str) -> Iterator[Page]:
    context = browser.new_context(viewport=PHONE, base_url=base_url, is_mobile=True, has_touch=True)
    yield context.new_page()
    context.close()  # an open event stream would hold the server's shutdown


def _fits(page: Page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"{page.url} scrolls sideways by {wide}px"


def _above_the_tab_bar(page: Page, button: str) -> None:
    save = page.get_by_role("button", name=button)
    expect(save).to_be_in_viewport(ratio=1)
    # the bar slides up on arrival; measure where it comes to rest
    save.evaluate("e => Promise.all(e.parentElement.getAnimations().map(a => a.finished))")
    box, bar = save.bounding_box(), page.locator(".tabbar").bounding_box()
    assert box and bar and box["y"] + box["height"] <= bar["y"], (box, bar)


def test_a_stage_is_edited_in_its_sheet_and_saved_from_the_pinned_bar(
    phone: Page, world: tuple[str, str]
) -> None:
    world_id, workflow = world
    phone.goto(f"/worlds/{world_id}/workflows")
    phone.get_by_role("link", name="Edit Phone flow").click()
    expect(phone.get_by_role("heading", name="Phone flow v1", level=1)).to_be_visible()
    expect(phone.get_by_text("Edit on a larger screen.")).to_have_count(0)
    # the save bar is pinned: in view on arrival, before any scrolling
    assert phone.evaluate("window.scrollY") == 0
    _above_the_tab_bar(phone, "Save as version 2")

    phone.get_by_test_id("stage-rows").get_by_role("button", name=re.compile("research")).click()
    sheet = phone.get_by_role("dialog", name="Stage settings")
    goal = sheet.get_by_label("Goal of research")
    expect(goal).to_have_value("Find 5 ideas.")
    goal.fill("Find 3 ideas with a hook each.")
    sheet.get_by_role("button", name="Done").click()
    expect(sheet).to_be_hidden()
    expect(phone.get_by_test_id("check-status")).to_have_text("Unsaved changes. Checks pass.")
    _above_the_tab_bar(phone, "Save as version 2")
    phone.get_by_role("button", name="Save as version 2").click()
    expect(phone.get_by_test_id("check-status")).to_contain_text("Saved as version 2")

    phone.reload()
    expect(phone.get_by_role("heading", name="Phone flow v2", level=1)).to_be_visible()
    expect(phone.get_by_test_id("stage-rows")).to_contain_text("Find 3 ideas with a hook each.")
    expect(phone).to_have_url(re.compile(f"/workflows/{workflow}"))


def test_a_new_workflow_is_made_from_a_starter_on_a_phone(
    phone: Page, world: tuple[str, str]
) -> None:
    world_id, _ = world
    phone.goto(f"/worlds/{world_id}/workflows/new")
    starters = phone.get_by_role("region", name="Starter workflows")
    starters.get_by_role("button", name=re.compile("^Weekly short")).click()
    expect(phone.get_by_role("heading", name="Weekly short", level=1)).to_be_visible()
    expect(phone.get_by_test_id("problems")).to_contain_text("Checks pass")
    _fits(phone)
    _above_the_tab_bar(phone, "Save as version 1")
    phone.get_by_role("button", name="Save as version 1").click()
    expect(phone).to_have_url(re.compile(r"/workflows/wf_"))
    expect(phone.get_by_role("heading", name="Weekly short v1", level=1)).to_be_visible()


@pytest.mark.parametrize("tab", ["stages", "planner", "yaml", "scorecard"])
def test_every_tab_fits_a_phone(phone: Page, world: tuple[str, str], tab: str) -> None:
    world_id, workflow = world
    phone.goto(f"/worlds/{world_id}/workflows/{workflow}?tab={tab}")
    expect(phone.get_by_role("tab", selected=True)).to_have_attribute("aria-selected", "true")
    expect(phone.get_by_role("heading", name="Phone flow v1", level=1)).to_be_visible()
    expect(phone.get_by_text("Loading...")).to_have_count(0)
    _fits(phone)
    _above_the_tab_bar(phone, "Save as version 2")
    if tab == "yaml":
        yaml = phone.get_by_label("Workflow YAML")
        assert yaml.evaluate("e => getComputedStyle(e).fontSize") == "16px"
