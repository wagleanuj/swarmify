"""Every page at phone width, behind basic auth, as an operator answering a gate from the sofa.

A page passes when it renders its content with no sideways scroll: nothing wider than the
screen. The run waits at a gate, so the timeline, gates inbox and agent log all have content."""

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

LOGIN = ("erlich", "aviato")  # what the `locked_url` app expects
PHONE: ViewportSize = {"width": 390, "height": 844}  # an iPhone 14
FLOW = """name: Brief then research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    tools: [web_search]
  - gate: pick
    ask: Which idea do we shoot?
"""
PAGES = [
    "/",
    "/gates",
    "/worlds/{world}",
    "/worlds/{world}/workflows",
    "/worlds/{world}/workflows/new",
    "/worlds/{world}/workflows/{workflow}",
    "/worlds/{world}/workflows/{workflow}/scorecard",
    "/worlds/{world}/runs",
    "/worlds/{world}/runs/{run}",
    "/worlds/{world}/connectors",
    "/worlds/{world}/graph",
    "/worlds/{world}/library",
    "/worlds/{world}/cast",
    "/worlds/{world}/settings",
    "/evals",
]


@pytest.fixture(scope="module")
def ids(locked_url: str) -> dict[str, str]:
    api = httpx.Client(base_url=f"{locked_url}/api", auth=LOGIN)
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    workflow = api.post(f"/worlds/{world}/workflows", json={"yaml": FLOW}).json()["workflow_id"]
    run = api.post(f"/workflows/{workflow}/runs", json={}).json()["id"]
    return {"world": world, "workflow": workflow, "run": run}


@pytest.fixture
def phone(browser: Browser, locked_url: str) -> Iterator[Page]:
    user, password = LOGIN
    context = browser.new_context(
        viewport=PHONE,
        http_credentials={"username": user, "password": password},
        base_url=locked_url,
        is_mobile=True,
        has_touch=True,
    )
    yield context.new_page()
    context.close()  # an open event stream would hold the server's shutdown


def test_without_signing_in_the_lan_sees_only_a_challenge(
    browser: Browser, locked_url: str
) -> None:
    context = browser.new_context()
    assert context.new_page().goto(locked_url).status == 401  # type: ignore[union-attr]
    context.close()
    assert httpx.get(f"{locked_url}/api/worlds").status_code == 401


@pytest.mark.parametrize("path", PAGES)
def test_every_page_fits_a_phone(phone: Page, ids: dict[str, str], path: str) -> None:
    phone.goto(path.format(**ids))
    # Event streams keep the network busy, so wait for the page's heading, not for idle.
    expect(phone.locator("h1").first).to_be_visible()
    expect(phone.get_by_text("Loading...")).to_have_count(0)
    assert phone.evaluate("window.innerWidth") == PHONE["width"]  # the viewport meta holds
    wide = phone.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    culprits = phone.evaluate(
        """[...document.querySelectorAll('body *')]
            .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
            .slice(0, 5).map(e => e.tagName + '.' + e.className)"""
    )
    assert wide <= 0, f"{path} scrolls sideways by {wide}px: {culprits}"
    # The sidebar gives way to a bottom tab bar; every tab and More's rows are a 44px thumb tap.
    expect(phone.get_by_role("navigation", name="Main")).to_be_hidden()
    tabs = phone.get_by_role("navigation", name="Tabs")
    heights = tabs.locator(".tabbar-item").evaluate_all("as => as.map(a => a.offsetHeight)")
    assert len(heights) == 5 and all(h >= 44 for h in heights), f"{path} has tabs {heights}px tall"
    tabs.get_by_role("button", name="More").click()
    rows = phone.locator(".sheet-row").evaluate_all("as => as.map(a => a.offsetHeight)")
    assert rows and all(h >= 44 for h in rows), f"{path} has More rows {rows}px tall"


def test_a_gate_is_answered_from_a_phone_as_the_stream_moves(phone: Page, ids: dict) -> None:
    phone.goto(f"/worlds/{ids['world']}/runs/{ids['run']}")
    status = phone.get_by_test_id("status-line")
    expect(status).to_have_text("Waiting for you: Which idea do we shoot?", timeout=15_000)
    phone.goto("/gates")
    card = phone.get_by_test_id(f"gate-{ids['run']}")
    expect(card).to_contain_text("Which idea do we shoot?")
    card.get_by_role("button", name="Approve").click()
    expect(phone.get_by_test_id("no-gates")).to_contain_text(
        "Nothing is waiting on you", timeout=15_000
    )
