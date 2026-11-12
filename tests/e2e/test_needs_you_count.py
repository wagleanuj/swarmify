"""One Needs-you count everywhere (issue 454): the sidebar badge, the phone's tab badge, a world's
overview, the Office HUD, its Needs you tab and Your office, and the inbox's world filter all read
the open gates the API lists, and agree."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect
from test_phone import PASSWORD, PHONE, signed_in
from test_phone import ids as ids  # noqa: F401

DESK: ViewportSize = {"width": 1440, "height": 900}


@pytest.fixture
def pages(browser: Browser, locked_url: str) -> Iterator[tuple[Page, Page]]:
    made = []
    for size, phone in ((DESK, False), (PHONE, True)):
        context = browser.new_context(
            viewport=size, base_url=locked_url, is_mobile=phone, has_touch=phone
        )
        context.request.post("/api/session", data={"password": PASSWORD})
        made.append(context)
    yield made[0].new_page(), made[1].new_page()
    for context in made:
        context.close()


def test_every_needs_you_count_reads_the_same_gates(
    pages: tuple[Page, Page], ids: dict[str, str], locked_url: str
) -> None:
    desk, phone = pages
    api = signed_in(locked_url)
    world = ids["world"]
    gates = api.get("/gates").json()
    total = len(gates)
    here = sum(g["world_id"] == world for g in gates)
    assert here >= 2, gates
    empty = api.post("/worlds", json={"name": "Nothing here"}).json()["id"]
    try:
        # A world's overview: the sidebar badge (every world) and its own Needs you block.
        desk.goto(f"/worlds/{world}")
        badge = desk.locator(".sidebar .nav-count")
        expect(badge).to_have_attribute("aria-label", f"{total} waiting")
        expect(desk.get_by_test_id("needs-you-count")).to_have_text(f"{here} waiting")
        answers = desk.get_by_test_id("needs-you").get_by_role("link", name="Answer")
        expect(answers).to_have_count(here)

        # The inbox filtered to the world: its option, its All chip and the place agree.
        desk.goto(f"/gates?world={world}")
        expect(desk.get_by_test_id("place")).to_have_text(re.compile(rf"^\s*1 of {here}\s*$"))
        every = desk.get_by_test_id("kinds").get_by_role("link", name=re.compile(r"^All"))
        expect(every).to_have_text(re.compile(rf"All\s*{here}"))
        expect(desk.get_by_role("combobox", name="World")).to_contain_text(f"All worlds ({total})")

        # A world with nothing open says so, and says how many wait elsewhere: no 0 against a badge.
        desk.goto(f"/gates?world={empty}")
        expect(desk.get_by_test_id("no-gates")).to_contain_text(
            f"{total} decision{'s wait' if total > 1 else ' waits'} in other worlds"
        )

        # The Office HUD, Your office and the Office's Needs you tab.
        desk.goto(f"/worlds/{world}/office")
        hud = desk.get_by_test_id("office-waiting")
        expect(hud).to_have_text(re.compile(rf"^{here} decisions waiting on you"))
        expect(desk.get_by_role("button", name=f"Your office, {here} waiting")).to_be_visible()
        expect(desk.locator(".office-tab", has_text="Needs you")).to_have_attribute(
            "aria-label", f"Needs you {here} waiting"
        )

        # The phone's tab badge.
        phone.goto(f"/worlds/{world}/runs")
        tab = phone.locator(".tabbar .tabbar-badge")
        expect(tab).to_have_attribute("aria-label", f"{total} waiting")
        phone.goto(f"/worlds/{world}/office")
        expect(phone.locator(".office-tab", has_text="Needs you")).to_have_attribute(
            "aria-label", f"Needs you {here} waiting"
        )
    finally:
        api.delete(f"/worlds/{empty}")
