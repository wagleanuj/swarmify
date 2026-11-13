"""The installed app never loads a document again once open: every tap is the router's, and a
pull to refresh refetches the page's data instead of reloading it (Chrome's loading bar is a
document load). On an Android phone, as the operator uses it; signing in is outside the sweep."""

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Frame, Page, Playwright, Request, expect
from test_phone import PASSWORD
from test_phone import ids as ids  # noqa: F401
from test_phone_nav import LONG_NAME, tab
from test_phone_nav import nav as nav  # noqa: F811


class Loads:
    """From `on`, every document the page's main frame requests or loads, every URL it navigates
    to (same-document ones too), each checked against the manifest's scope (a navigation out of
    it opens Chrome's custom tab, loading bar and all), and every window the app opens."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.documents: list[str] = []
        self.loads = 0
        self.urls: list[str] = []
        self.windows: list[str] = []
        page.on("request", self._request)
        page.on("load", lambda _: self._load())
        page.on("framenavigated", self._navigated)
        page.context.on("page", self._window)
        self.on = False

    def _request(self, request: Request) -> None:
        if self.on and request.is_navigation_request() and request.frame == self.page.main_frame:
            self.documents.append(request.url)

    def _window(self, page: Page) -> None:
        if self.on:
            self.windows.append(page.url)

    def _load(self) -> None:
        if self.on:
            self.loads += 1

    def _navigated(self, frame: Frame) -> None:
        if self.on and frame == self.page.main_frame:
            self.urls.append(frame.url)

    def check(self, what: str, origin: str) -> None:
        # The manifest's scope is "/": every in-app URL is on the app's origin.
        out = [u for u in self.urls if not u.startswith(origin + "/")]
        assert not out, f"after {what}: left the manifest scope for {out}"
        assert not self.windows, f"after {what}: opened a window at {self.windows}"
        # A same-origin link that opens a window would load the app again in Chrome's custom tab.
        windowed = self.page.evaluate(
            """() => [...document.querySelectorAll('a[target="_blank"]')]
                .filter((a) => a.origin === location.origin && !a.pathname.startsWith('/api/'))
                .map((a) => a.pathname)"""
        )
        assert not windowed, f"after {what}: in-app links that open a window: {windowed}"
        assert not self.documents and not self.loads, f"after {what}: loaded {self.documents}"


@pytest.fixture
def android(browser: Browser, playwright: Playwright, locked_url: str) -> Iterator[Page]:
    context = browser.new_context(**playwright.devices["Pixel 7"], base_url=locked_url)
    context.request.post("/api/session", data={"password": PASSWORD})
    page = context.new_page()
    yield page
    context.close()  # an open event stream would hold the server's shutdown


def test_start_url_loads_without_a_redirect(locked_url: str) -> None:
    manifest = httpx.get(f"{locked_url}/manifest.webmanifest").json()
    assert manifest["id"] and manifest["scope"] == "/" and manifest["start_url"].startswith("/")
    start = httpx.get(f"{locked_url}{manifest['start_url']}", follow_redirects=False)
    assert start.status_code == 200 and 'rel="manifest"' in start.text


def test_tapping_round_the_app_never_loads_a_document(
    android: Page, locked_url: str, nav: dict
) -> None:  # noqa: F811
    page = android
    world = nav["world"]
    page.goto(f"/worlds/{world}/runs")
    expect(page.get_by_test_id("run-list")).to_be_visible()
    loads = Loads(page)
    loads.on = True
    origin = locked_url

    for name in ["Workflows", "Library", "Needs you", "Runs"]:
        tab(page, name)
        expect(page.locator("h1").first).to_be_visible()
        loads.check(f"tapping {name}", origin)
    for row in [
        "Overview",
        "Assets",
        "Memory",
        "Cast",
        "Connectors",
        "Evals",
        "Settings",
        "All worlds",
    ]:
        tab(page, "More")
        sheet = page.get_by_role("dialog", name="More")
        sheet.get_by_role("link", name=row).click()
        expect(sheet).to_be_hidden()
        loads.check(f"More, {row}", origin)

    tab(page, "Runs")
    page.get_by_test_id("run-list").get_by_role("link").first.click()
    expect(page).to_have_url(re.compile(r"/runs/[^/?]+"))
    loads.check("opening a run", origin)
    page.go_back()
    expect(page.get_by_test_id("run-list")).to_be_visible()
    loads.check("back to Runs", origin)

    tab(page, "Needs you")
    deck = page.get_by_role("list", name="Decisions")
    expect(deck).to_be_visible()
    deck.get_by_role("link").first.click()
    loads.check("opening a gate's run", origin)

    tab(page, "Workflows")
    page.get_by_role("link", name=LONG_NAME).first.click()
    expect(page).to_have_url(re.compile(r"/workflows/[^/?]+"))
    loads.check("opening the editor", origin)
    assert len(loads.urls) > 15, loads.urls  # the sweep really did move round the app


def pull(page: Page, x: int, y: int, by: int) -> None:
    """A finger down at x, y, dragged `by` px down over a third of a second, then lifted."""
    cdp = page.context.new_cdp_session(page)
    touch = lambda kind, at: cdp.send(  # noqa: E731
        "Input.dispatchTouchEvent",
        {"type": kind, "touchPoints": [{"x": x, "y": at}] if kind != "touchEnd" else []},
    )
    touch("touchStart", y)
    for step in range(1, 21):
        touch("touchMove", y + by * step // 20)
        page.wait_for_timeout(16)
    touch("touchEnd", y + by)


def test_pulling_down_runs_refetches_them_without_a_reload(
    browser: Browser, locked_url: str, nav: dict
) -> None:  # noqa: F811
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, base_url=locked_url
    )
    context.request.post("/api/session", data={"password": PASSWORD})
    page = context.new_page()
    try:
        page.goto(f"/worlds/{nav['world']}/runs")
        expect(page.get_by_test_id("run-list")).to_be_visible()
        loads = Loads(page)
        loads.on = True
        origin = locked_url
        asked: list[str] = []
        page.on("request", lambda r: asked.append(r.url))
        listing = f"/api/worlds/{nav['world']}/runs"

        pull(page, 195, 300, 60)  # short of the threshold: springs back, nothing fetched
        page.wait_for_timeout(500)
        assert not [u for u in asked if u.endswith(listing)]

        asked.clear()
        pull(page, 195, 300, 260)
        expect(page.get_by_role("status").filter(has_text="Updated just now")).to_be_attached()
        assert [u for u in asked if u.endswith(listing)], "the pull did not refetch the runs"
        assert [u for u in asked if "/worlds/" in u and "/events?since=" in u], (
            "the pull did not reopen the event stream"
        )
        loads.check("pulling to refresh", origin)
    finally:
        context.close()
