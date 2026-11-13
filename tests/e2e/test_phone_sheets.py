"""Bottom sheets behave like a phone's own: drag the More sheet by its grabber past a third of its
height and it closes, a short drag springs back, a quick flick down closes it, and Android's back
closes it without leaving the page. Real touches, through the DevTools protocol."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, CDPSession, Locator, Page, expect
from test_phone import ids as ids  # noqa: F401
from test_phone_nav import phone_at


@pytest.fixture
def phone(browser: Browser, locked_url: str, ids: dict[str, str]) -> Iterator[Page]:  # noqa: F811
    for page in phone_at(browser, locked_url, {"width": 390, "height": 844}):
        page.goto(f"/worlds/{ids['world']}/runs")
        expect(page.locator("h1").first).to_be_visible()
        yield page


def more(page: Page) -> Locator:
    page.get_by_role("navigation", name="Tabs").get_by_role("button", name="More").click()
    sheet = page.get_by_role("dialog", name="More")
    expect(sheet).to_be_visible()
    page.wait_for_timeout(500)  # it has slid all the way up
    return sheet


def drag(page: Page, sheet: Locator, dy: float, steps: int, pause: int) -> None:
    """A finger down on the grabber, `steps` moves to `dy` below it `pause` ms apart, then up."""
    box = sheet.locator(".sheet-handle").bounding_box()
    assert box
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp: CDPSession = page.context.new_cdp_session(page)

    def touch(kind: str, at: float | None) -> None:
        points = [] if at is None else [{"x": x, "y": at}]
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    touch("touchStart", y)
    for i in range(1, steps + 1):
        touch("touchMove", y + dy * i / steps)
        if pause:
            page.wait_for_timeout(pause)
    touch("touchEnd", None)


def test_dragging_the_grabber_past_the_threshold_closes_the_sheet(phone: Page) -> None:
    sheet = more(phone)
    height = sheet.bounding_box()["height"]  # type: ignore[index]
    drag(phone, sheet, height * 0.45, steps=15, pause=25)
    expect(sheet).to_be_hidden()
    expect(phone.locator("dialog[open]")).to_have_count(0)


def test_a_short_drag_springs_back_and_stays_open(phone: Page) -> None:
    sheet = more(phone)
    top = sheet.bounding_box()["y"]  # type: ignore[index]
    drag(phone, sheet, 60, steps=6, pause=40)
    phone.wait_for_timeout(600)
    expect(sheet).to_be_visible()
    assert abs(sheet.bounding_box()["y"] - top) < 1  # type: ignore[index]
    # And a row still opens its page.
    sheet.get_by_role("link", name="Evals").click()
    expect(phone).to_have_url(re.compile(r"/evals$"))
    phone.wait_for_timeout(500)  # closing the sheet does not step back off the new page
    expect(phone).to_have_url(re.compile(r"/evals$"))


def test_a_fast_flick_down_closes_the_sheet(phone: Page) -> None:
    sheet = more(phone)
    drag(phone, sheet, 80, steps=3, pause=0)
    expect(sheet).to_be_hidden()


def test_android_back_closes_the_sheet_and_stays_on_the_page(phone: Page) -> None:
    url = phone.url
    sheet = more(phone)
    phone.go_back()
    expect(sheet).to_be_hidden()
    assert phone.url == url
    # Closed by a tap on the backdrop, it leaves no step behind in history.
    sheet = more(phone)
    phone.mouse.click(195, 40)
    expect(sheet).to_be_hidden()
    phone.wait_for_timeout(300)
    assert phone.url == url
