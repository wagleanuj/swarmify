"""A new world's first load and its empty places. While a route's first requests are still out it
shows skeletons, never an empty state; once they land, each empty place is its glyph, one
sentence and one action, at 390 in the plain view and the Office."""

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect
from test_phone import FLOW, PHONE

ROUTES = [
    # (path, whether the empty world shows an empty state there)
    ("/worlds/{empty}", True),
    ("/worlds/{empty}/runs", True),
    ("/worlds/{empty}/workflows", True),
    ("/worlds/{empty}/library", True),
    ("/worlds/{empty}/graph", True),
    ("/worlds/{empty}/assets", True),
    ("/gates?world={empty}", True),
    ("/evals", True),
    ("/worlds/{empty}/cast", False),
    ("/worlds/{empty}/connectors", False),
    ("/worlds/{empty}/settings", False),
    ("/worlds/{world}/runs/{run}", False),
]

SHAPE = """() => [...document.querySelectorAll('main .empty')].map(e => {
  const panel = e.closest('.panel');
  const action = e.querySelector('a, button, label.button')
    || panel?.querySelector(':scope > .panel-head :is(a, button, label.button)');
  return {
    where: e.dataset.testid || e.className,
    icons: e.querySelectorAll(':scope > svg.icon').length,
    sentences: e.querySelectorAll(':scope > p, :scope > details.intro').length,
    action: action ? action.textContent.trim() : '',
  };
})"""


@pytest.fixture(scope="module")
def worlds(base_url: str) -> Iterator[dict[str, str]]:
    api = httpx.Client(base_url=f"{base_url}/api", timeout=30)
    empty = api.post("/worlds", json={"name": "Nothing yet"}).json()["id"]
    world = api.post("/worlds", json={"name": "Busy"}).json()["id"]
    try:
        wf = api.post(f"/worlds/{world}/workflows", json={"yaml": FLOW}).json()["workflow_id"]
        run = api.post(f"/workflows/{wf}/runs", json={}).json()["id"]
        yield {"empty": empty, "world": world, "run": run}
    finally:
        # The server is shared with the modules after this one: leave no world and no open gate.
        for id in (empty, world):
            api.delete(f"/worlds/{id}")


@pytest.fixture
def phone(browser: Browser, base_url: str) -> Iterator[Page]:
    context = browser.new_context(viewport=PHONE, base_url=base_url, is_mobile=True, has_touch=True)
    yield context.new_page()
    context.close()


def test_no_route_says_it_is_empty_before_its_first_answer(
    phone: Page, worlds: dict[str, str]
) -> None:
    held: list[Route] = []
    holding = True

    def hold(route: Route) -> None:
        # The sign-in check and the event streams go through; every read waits until let go.
        url = route.request.url
        if not holding or "/api/session" in url or "/events" in url:
            route.continue_()
        else:
            held.append(route)

    phone.route("**/api/**", hold)
    for path, empty in ROUTES:
        holding = True
        phone.goto(path.format(**worlds))
        expect(phone.locator("main [aria-busy='true']").first).to_be_attached()
        phone.wait_for_timeout(400)
        assert phone.locator("main .empty").count() == 0, f"{path} says empty while loading"
        holding = False
        for r in held:
            try:
                r.continue_()
            except Exception:  # noqa: BLE001  a request the navigation already dropped
                pass
        held.clear()
        expect(phone.locator("main [aria-busy='true']")).to_have_count(0, timeout=15_000)
        if not empty:
            continue
        expect(phone.locator("main .empty").first).to_be_visible()
        for place in phone.evaluate(SHAPE):
            assert place["icons"] == 1, f"{path} {place}"
            assert place["sentences"] == 1, f"{path} {place}"
            assert place["action"], f"{path} {place}"


def test_the_office_needs_you_when_clear_is_the_crew_a_sentence_and_a_way_back(
    phone: Page, worlds: dict[str, str], base_url: str
) -> None:
    api = httpx.Client(base_url=f"{base_url}/api", timeout=30)
    w = api.get(f"/worlds/{worlds['empty']}").json()
    api.patch(
        f"/worlds/{worlds['empty']}", json={"skin": {**(w.get("skin") or {}), "view": "office"}}
    ).raise_for_status()
    try:
        phone.goto(f"/worlds/{worlds['empty']}/office/talk")
        clear = phone.get_by_test_id("talk-clear")
        expect(clear.get_by_role("heading", name="All clear")).to_be_visible(timeout=15_000)
        expect(clear.locator(".talk-crew .office-actor").first).to_be_visible()
        expect(clear.locator("p")).to_have_count(1)
        expect(clear.get_by_role("link", name="Back to the office")).to_be_visible()
    finally:
        api.patch(
            f"/worlds/{worlds['empty']}", json={"skin": {**(w.get("skin") or {}), "view": "plain"}}
        )
