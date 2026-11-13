"""Tap through the app on a phone, the way the operator does, and watch the page's proportions.

The phone audit loads each route straight; this sweep only taps: every bottom tab in turn, More's
rows, a run from Runs and back, a gate from Needs you, the editor and a dialog. A sampler polls
every 50 ms the whole time, so a page that runs wider than the screen even for a frame of a
transition (and a phone then zooms the whole app out) fails, as does one that settles wide."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect
from test_phone import PASSWORD, signed_in
from test_phone import ids as ids  # noqa: F401

SIZES: list[ViewportSize] = [{"width": 390, "height": 844}, {"width": 360, "height": 740}]
# A real world's workflows have long names; a select sizes itself to its longest option.
LONG_NAME = "A workflow whose name runs on well past the width of any phone screen"
SAMPLER = """(() => {
  window.__wide = [];
  const W = %d;
  setInterval(() => {
    const v = visualViewport, b = document.body.getBoundingClientRect();
    const wide = Math.max(document.documentElement.scrollWidth, innerWidth, Math.round(b.width));
    if (wide > W || Math.abs(v.scale - 1) > 0.01 || Math.round(v.width) !== W)
      window.__wide.push(`${location.pathname}: ${wide}px wide, scale ${v.scale.toFixed(2)}`);
  }, 50);
})()"""


@pytest.fixture(scope="module")
def nav(locked_url: str, ids: dict[str, str]) -> dict[str, str]:  # noqa: F811
    api = signed_in(locked_url)
    world = ids["world"]
    yaml = f"name: {LONG_NAME}\nstages:\n  - gate: go\n    ask: Go?\n"
    api.post(f"/worlds/{world}/workflows", json={"yaml": yaml}).raise_for_status()
    # The second theme: a world drawn as the Office.
    office = api.post("/worlds", json={"name": "Office"}).json()["id"]
    w = api.get(f"/worlds/{office}").json()
    api.patch(
        f"/worlds/{office}", json={"skin": {**(w.get("skin") or {}), "view": "office"}}
    ).raise_for_status()
    return {**ids, "office": office}


def phone_at(browser: Browser, url: str, size: ViewportSize) -> Iterator[Page]:
    context = browser.new_context(
        viewport=size,
        base_url=url,
        is_mobile=True,  # a page wider than the screen is zoomed out, as on a phone
        has_touch=True,
        device_scale_factor=3,
    )
    context.request.post("/api/session", data={"password": PASSWORD})
    page = context.new_page()
    page.add_init_script(SAMPLER % size["width"])
    yield page
    context.close()


@pytest.fixture(params=SIZES, ids=["390", "360"])
def phone(browser: Browser, locked_url: str, request: pytest.FixtureRequest) -> Iterator[Page]:
    yield from phone_at(browser, locked_url, request.param)


def step(page: Page, what: str) -> None:
    """Wait out the transition (1 s of samples), then fail on any wide or zoomed sample."""
    page.wait_for_timeout(1000)
    wide = page.evaluate("() => window.__wide.splice(0)")
    assert not wide, f"after {what}: {sorted(set(wide))}"


def tab(page: Page, name: str) -> None:
    tabs = page.get_by_role("navigation", name="Tabs")
    role = "button" if name == "More" else "link"
    # "Needs you" carries its badge's count in its name: "Needs you 3 waiting".
    tabs.get_by_role(role, name=re.compile(f"^{name}")).click(timeout=3000)  # a real tap, no force


@pytest.mark.parametrize("theme", ["plain", "office"])
def test_tapping_round_the_app_keeps_it_screen_wide(phone: Page, nav: dict, theme: str) -> None:
    world = nav["world"] if theme == "plain" else nav["office"]
    phone.goto("/gates")
    expect(phone.locator("h1").first).to_be_visible()
    step(phone, "opening Needs you")
    phone.goto(f"/worlds/{world}")
    step(phone, "opening the world")
    places = {
        "Runs": f"/worlds/{world}/runs",
        "Workflows": f"/worlds/{world}/workflows",
        "Library": f"/worlds/{world}/library",
        "Needs you": "/gates",
    }
    order = [
        "Runs",
        "Workflows",
        "Library",
        "Needs you",
        "Library",
        "Runs",
        "Needs you",
        "Workflows",
        "Runs",
    ]
    for name in order:
        tab(phone, name)
        expect(phone).to_have_url(re.compile(re.escape(places[name]) + r"([?#].*)?$"))
        step(phone, f"tapping {name}")
    rows = ["Overview", "Assets", "Memory", "Cast", "Connectors", "Evals", "Settings", "All worlds"]
    for row in rows:
        if theme == "office" and row == "Overview":
            continue  # the Office, with a bar of its own; opened by Overview below
        tab(phone, "More")
        sheet = phone.get_by_role("dialog", name="More")
        sheet.get_by_role("link", name=row).click(timeout=3000)
        expect(sheet).to_be_hidden()
        step(phone, f"More, {row}")
    if theme == "office":
        phone.goto(f"/worlds/{world}/runs")
        tab(phone, "More")
        phone.get_by_role("dialog", name="More").get_by_role("link", name="Overview").click()
        expect(phone).to_have_url(re.compile(r"/office"))
        step(phone, "More, Overview, the Office")
    if theme == "plain":
        # A run from Runs, then back.
        phone.goto(f"/worlds/{world}/runs")
        tab(phone, "Runs")
        phone.get_by_test_id("run-list").get_by_role("link").first.click()
        expect(phone).to_have_url(re.compile(r"/runs/[^/?]+"))
        step(phone, "opening a run")
        phone.go_back()
        step(phone, "back to Runs")
        # A gate from Needs you, then its Send back dialog.
        tab(phone, "Needs you")
        expect(phone.get_by_role("list", name="Decisions")).to_be_visible()
        step(phone, "opening a gate")
        # A gate with a Send back: step 2 of the long run.
        phone.goto(f"/gates?gate={nav['long_gate']}")
        card = phone.get_by_test_id(f"gate-{nav['long']}")
        card.get_by_role("button", name=re.compile("^Send back")).click()
        step(phone, "the send back dialog")
        phone.keyboard.press("Escape")
        # The editor.
        tab(phone, "Workflows")
        phone.get_by_role("link", name=LONG_NAME).first.click()
        step(phone, "opening the editor")
