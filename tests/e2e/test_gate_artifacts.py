"""Stories 53 and 54 in the browser, at desktop and phone width: the pick gate offers the
research stage's idea cards and the pick names the card; the review gate plays the rendered
video in place before the decision, and both decision files record the artifact. The scripted
app's judge ranks two ideas and its render writes short.mp4 with a video sidecar."""

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

from scripted_app import GOLDEN

WIDTHS: dict[str, ViewportSize] = {
    "desktop": {"width": 1280, "height": 900},
    "phone": {"width": 390, "height": 844},
}


@pytest.fixture(params=list(WIDTHS))
def screen(request: pytest.FixtureRequest, browser: Browser, base_url: str) -> Iterator[Page]:
    size = WIDTHS[request.param]
    phone = size["width"] < 500
    context = browser.new_context(
        viewport=size, base_url=base_url, is_mobile=phone, has_touch=phone
    )
    yield context.new_page()
    context.close()


def _fits(page: Page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the gates page scrolls sideways by {wide}px"


def test_pick_a_card_then_review_the_video_in_place(
    screen: Page, base_url: str, data_dir: Path
) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Cards"}).json()["id"]
    try:
        wf = httpx.post(f"{base_url}/api/worlds/{world}/workflows", json={"yaml": GOLDEN}).json()
        run = httpx.post(f"{base_url}/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        screen.goto("/gates")
        card = screen.get_by_test_id(f"gate-{run['id']}")
        expect(card).to_contain_text("Pick one idea.", timeout=20_000)
        cards = card.get_by_test_id("pick-card")
        expect(cards).to_have_count(2)
        expect(cards.first).to_contain_text("Erlich's #1 of 2")
        expect(cards.first).to_contain_text("Hook first")
        expect(cards.first).to_contain_text("Wait for it")
        expect(cards.first).to_contain_text("Why: Hooks in the first second win.")
        expect(cards.nth(1)).to_contain_text("The chase")
        expect(card.get_by_role("textbox", name="Your pick")).to_have_count(0)
        _fits(screen)
        card.get_by_role("radio", name=re.compile("The chase")).click()
        card.get_by_role("button", name="Continue with this pick").click()

        expect(card).to_contain_text("Watch it. Approve or send back.", timeout=20_000)
        player = card.get_by_test_id("player")
        expect(player).to_be_visible()
        src = player.get_attribute("src") or ""
        assert src.endswith("/artifacts/short.mp4")
        assert httpx.get(f"{base_url}{src}").content == b"not really an mp4"
        _fits(screen)
        card.get_by_role("button", name="Approve").click()
        expect(card).to_contain_text("Post it?", timeout=20_000)

        folder = data_dir / "worlds" / world / "runs" / run["id"]
        decisions = {p.parent.name: p.read_text() for p in folder.rglob("decision.md")}
        gates = [s["id"] for s in run["stages"] if s["type"] == "gate"]
        pick, review = (decisions[g] for g in gates[:2])  # the confirm gate is still open
        research, render = run["stages"][0]["id"], run["stages"][2]["id"]
        assert 'kind: "pick"' in pick and "Picked The chase." in pick
        assert f'artifact: "runs/{run["id"]}/{research}/artifacts/idea-the-chase.md"' in pick
        assert "Approved." in review
        # A binary is named by its sidecar, as the Library lists it.
        assert f'artifact: "runs/{run["id"]}/{render}/artifacts/short.mp4.md"' in review

        # On the run page the answered review step still shows the video it approved, and the
        # render step names what went in.
        screen.goto(f"/worlds/{world}/runs/{run['id']}?stage={gates[1]}")
        reviewed = screen.get_by_test_id("gate-panel").get_by_test_id("player")
        expect(reviewed).to_have_attribute("src", re.compile(r"/artifacts/short\.mp4$"))
        screen.goto(f"/worlds/{world}/runs/{run['id']}?stage={render}")
        expect(screen.get_by_test_id("inputs")).to_contain_text("ideas.md")
        _fits(screen)
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world}")
