"""Every route at two phone sizes, with every menu, sheet and dialog it opens: nothing wider than
the screen, no tap target under 44 px, no text spilling out of its box, nothing fixed over the
tab bar, and no browser confirm. Each finding names the route, the overlay, the check and the
element, so a regression says where to look."""

import json
import re
import time
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Dialog, Locator, Page, ViewportSize, expect
from playwright.sync_api import Error as PlaywrightError
from test_phone import FLOW, PASSWORD, signed_in
from test_phone import ids as ids  # noqa: F401

SIZES: list[ViewportSize] = [{"width": 390, "height": 844}, {"width": 360, "height": 740}]
BROKEN = """name: Breaks
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    produces: [missing.md]
  - connector: render
    calls: remotion_render
    input: missing.md
    output: short.mp4
    params: {project: shorts, composition: Short}
"""
"""Its research never writes missing.md, so the render has nothing to read and the run fails."""
ROUTES = [
    "/",
    "/gates",
    "/gates?gate={long_gate}",
    "/evals",
    "/worlds/{world}",
    "/worlds/{world}/office",
    "/worlds/{world}/office/crew",
    "/worlds/{world}/office/talk",
    "/worlds/{world}/workflows",
    "/worlds/{world}/workflows/new",
    "/worlds/{world}/workflows/{workflow}",
    "/worlds/{world}/workflows/{workflow}/scorecard",
    "/worlds/{world}/runs",
    "/worlds/{world}/runs/{run}",
    "/worlds/{world}/runs/{long}",
    "/worlds/{world}/runs/{running}",
    "/worlds/{world}/runs/{done}",
    "/worlds/{world}/runs/{paused}",
    "/worlds/{world}/runs/{failed}",
    "/worlds/{world}/read?path={doc}",
    "/worlds/{world}/connectors",
    "/worlds/{world}/graph",
    "/worlds/{world}/library",
    "/worlds/{world}/assets",
    "/worlds/{world}/cast",
    "/worlds/{world}/settings",
]

CHECKS = """W => {
  // The phone's width, not innerWidth: a phone zooms the page out to fit what runs wide.
  const out = [];
  const add = (check, e, detail) => out.push({check, element: name(e), detail});
  const name = e => {
    if (typeof e === 'string') return e;
    const p = [];
    for (let n = e; n && n !== document.body && p.length < 3; n = n.parentElement) {
      const cls = typeof n.className === 'string' ? n.className.trim().split(/\\s+/) : [];
      const id = n.dataset && n.dataset.testid ? `[data-testid=${n.dataset.testid}]` : '';
      p.unshift(n.tagName.toLowerCase() + cls.slice(0, 2).map(c => c && '.' + c).join('') + id);
    }
    return p.join(' > ');
  };
  const ancestors = e => {
    const all = [];
    for (let n = e.parentElement; n && n !== document.body; n = n.parentElement) all.push(n);
    return all;
  };
  // Shown, and not a screen reader's 1px clipped copy.
  const shown = e => e.checkVisibility() && !e.closest('dialog:not([open]), [inert]')
    && ![e, ...ancestors(e)].some(n => getComputedStyle(n).clipPath !== 'none');
  const clipsX = e => ancestors(e).some(n => getComputedStyle(n).overflowX !== 'visible');
  const wide = Math.max(document.documentElement.scrollWidth, innerWidth) - W;
  if (wide > 0) add('overflow', 'html', `${wide}px sideways`);
  // The outermost element past either edge, unless a scroller holds it.
  const past = e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && (r.right > W + 1 || r.left < -1);
  };
  for (const e of document.querySelectorAll('body *')) {
    if (!shown(e) || !past(e) || clipsX(e) || (e.parentElement && past(e.parentElement))) continue;
    const r = e.getBoundingClientRect();
    add('wider than the screen', e, `${Math.round(r.left)} to ${Math.round(r.right)}`);
  }
  const taps = ['button:not(.link)', 'a.button', 'label.button', 'select', '.tab', 'summary',
    '[role=menuitem]', 'input[type=checkbox]', 'input[type=radio]'].join(', ');
  for (const e of document.querySelectorAll(taps)) {
    if (!shown(e) || e.closest('.pill, .chip, .status')) continue;
    const t = e.matches('input') ? (e.closest('label') || e) : e;
    const h = t.getBoundingClientRect().height;
    const text = (t.textContent || t.getAttribute('aria-label') || '').trim().slice(0, 24);
    if (h < 43.5) add('tap under 44px', t, `${Math.round(h)}px "${text}"`);
  }
  const own = 'input, textarea, select, pre, code, video, img, svg, canvas, .scroll-x';
  for (const e of document.querySelectorAll('body *')) {
    if (!shown(e) || e.matches(own)) continue;
    if (![...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const s = getComputedStyle(e);
    if (e.scrollWidth <= e.clientWidth + 1) continue;
    if (s.textOverflow === 'ellipsis' || s.display === 'inline') continue;
    const check = s.overflowX === 'visible' ? 'text spills out' : 'text clipped';
    add(check, e, `${e.scrollWidth} in ${e.clientWidth}px`);
  }
  const bar = document.querySelector('.tabbar');
  if (bar && bar.checkVisibility()) {
    const b = bar.getBoundingClientRect();
    for (const e of document.querySelectorAll('body *')) {
      if (bar.contains(e) || e.contains(bar) || e.closest('dialog[open]') || !shown(e)) continue;
      if (!['fixed', 'sticky'].includes(getComputedStyle(e).position)) continue;
      const r = e.getBoundingClientRect();
      const hit = r.bottom > b.top + 1 && r.top < b.bottom && r.right > b.left && r.left < b.right;
      if (r.width && r.height && hit) add('over the tab bar', e, `bottom ${Math.round(r.bottom)}`);
    }
  }
  for (const e of document.querySelectorAll('[role=dialog], [role=alertdialog]'))
    if (e.tagName !== 'DIALOG' && shown(e)) add('ad hoc dialog', e, '');
  return out;
}"""
DESTRUCTIVE = re.compile(r"^(Abort|Delete|Uninstall|Disable|Revoke)")
"""Buttons whose action waits on a confirm; a danger button that only edits a draft is left be."""
OPENERS = ", ".join(
    [
        "main button[aria-haspopup]",
        "main button[aria-expanded]",
        "main summary",
        ".topbar button[aria-haspopup]",
    ]
)


def settle(page: Page) -> None:
    expect(page.locator("h1").first).to_be_visible()
    expect(page.locator("main [aria-busy='true']")).to_have_count(0, timeout=15_000)
    page.wait_for_timeout(600)  # the last fetches land and the entrance motion ends


def audit(page: Page, route: str, confirms: list[str], seen: list[str]) -> list[dict[str, str]]:
    """Check the page as it stands, then with each overlay on it open in turn: its toggles, then
    each danger action, cancelled. A browser confirm is a finding of its own."""
    found: list[dict[str, str]] = []

    def check(where: str) -> None:
        for f in page.evaluate(CHECKS, page.viewport_size["width"]):  # type: ignore[index]
            found.append({"route": route, "where": where, **f})
        seen.append(f"{route} [{where}]")
        for m in confirms:
            found.append({"route": route, "where": where, "check": "browser confirm", "element": m})
        confirms.clear()

    def tap(target: Locator, what: str) -> bool:
        """Tap it as a thumb would; something drawn over it is a finding."""
        try:
            target.click(timeout=3000)
            return True
        except PlaywrightError as e:
            covered = next((x.strip()[:120] for x in str(e).split("\n") if "intercepts" in x), "")
            found.append({"route": route, "where": what, "check": "covered", "element": covered})
            page.keyboard.press("Escape")
            return False

    def close() -> None:
        page.keyboard.press("Escape")
        page.wait_for_timeout(350)  # a sheet slides out

    def cancel() -> None:
        dialog = page.locator("dialog.dialog[open]")
        if dialog.count():
            dialog.get_by_role("button", name="Cancel").click()
            expect(dialog).to_have_count(0)

    check("page")
    url = page.url
    # Each destructive button asks first; look at what it asks, then cancel.
    danger = page.locator("main button.danger").filter(has_text=DESTRUCTIVE)
    for button in danger.filter(visible=True).all():
        what = button.inner_text().strip()
        if tap(button, what):
            page.wait_for_timeout(350)
            check(what)
            cancel()
    for i in range(page.locator(OPENERS).filter(visible=True).count()):
        toggle = page.locator(OPENERS).filter(visible=True).nth(i)
        if not toggle.count() or not toggle.is_enabled():
            continue
        label = (toggle.get_attribute("aria-label") or toggle.inner_text()).strip()[:30]
        if not tap(toggle, label):
            continue
        page.wait_for_timeout(350)
        check(f"open: {label}")
        for item in page.locator("[role=menuitem].danger").filter(visible=True).all():
            item_label = item.inner_text().split("\n")[0]
            if not tap(item, f"{label} > {item_label}"):
                break
            page.wait_for_timeout(350)
            check(f"{label} > {item_label}")
            cancel()
            toggle.click()  # the menu again, for the next item
            page.wait_for_timeout(200)
        if page.url != url:
            page.goto(url)
            settle(page)
            continue
        if toggle.get_attribute("aria-expanded") == "true":
            close()
            if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "true":
                toggle.click()
    return found


@pytest.fixture(scope="module")
def every(ids: dict[str, str], locked_url: str) -> dict[str, str]:
    """The phone suite's world, plus a paused run and a failed one."""
    api = signed_in(locked_url)
    world = ids["world"]

    def start(yaml: str, status: str) -> str:
        wf = api.post(f"/worlds/{world}/workflows", json={"yaml": yaml}).json()["workflow_id"]
        run = api.post(f"/workflows/{wf}/runs", json={}).json()["id"]
        for _ in range(200):
            if api.get(f"/runs/{run}").json()["status"] == status:
                return run
            time.sleep(0.1)
        raise AssertionError(f"never reached {status}")

    paused = start(FLOW.replace("Brief then research", "Paused"), "waiting_gate")
    api.post(f"/runs/{paused}/pause").raise_for_status()
    return {**ids, "paused": paused, "failed": start(BROKEN, "failed")}


@pytest.fixture(params=SIZES, ids=lambda s: f"{s['width']}x{s['height']}")
def page(request: pytest.FixtureRequest, browser: Browser, locked_url: str) -> Iterator[Page]:
    context = browser.new_context(
        viewport=request.param, base_url=locked_url, is_mobile=True, has_touch=True
    )
    context.request.post("/api/session", data={"password": PASSWORD})
    yield context.new_page()
    context.close()


def test_every_route_and_overlay_fits_a_phone(page: Page, every: dict[str, str]) -> None:
    confirms: list[str] = []

    def dismiss(d: Dialog) -> None:
        confirms.append(d.message)
        d.dismiss()

    page.on("dialog", dismiss)
    found: list[dict[str, str]] = []
    seen: list[str] = []
    for route in ROUTES:
        page.goto(route.format(**every))
        settle(page)
        found += audit(page, route, confirms, seen)
    page.context.clear_cookies()
    page.goto("/gates")
    expect(page.get_by_label("Password")).to_be_visible()
    found += audit(page, "sign in", confirms, seen)
    key = lambda f: json.dumps({k: v for k, v in f.items() if k != "where"}, sort_keys=True)  # noqa: E731
    unique = {key(f): f for f in found}
    report = "\n".join(
        f"{f['route']} [{f['where']}] {f['check']}: {f['element']} {f.get('detail', '')}"
        for f in unique.values()
    )
    print(f"\nAUDIT {len(seen)} states, {len(unique)} findings\n{report}")
    assert not unique, f"{len(unique)} findings:\n{report}"
    # The walk reached the overlays: the abort confirm of the four live runs, and a world's delete.
    assert sum("Run actions > Abort run" in w for w in seen) == 4, seen
    assert any("/settings [Delete" in w for w in seen), seen


def test_aborting_from_a_phone_asks_in_a_dialog_that_fits(
    page: Page, every: dict[str, str], locked_url: str
) -> None:
    """The run menu opens inside the screen; Abort asks in a sheet no wider than the phone, focus
    on Cancel; Cancel keeps the run, Abort ends it."""
    api = signed_in(locked_url)
    run = api.post(f"/workflows/{every['workflow']}/runs", json={}).json()["id"]
    for _ in range(200):
        if api.get(f"/runs/{run}").json()["status"] == "waiting_gate":
            break
        time.sleep(0.1)
    page.goto(f"/worlds/{every['world']}/runs/{run}")
    settle(page)
    width = page.viewport_size["width"]  # type: ignore[index]
    wide = "Math.max(document.documentElement.scrollWidth, innerWidth)"
    assert page.evaluate(wide) == width
    page.get_by_role("button", name="Run actions").click()
    assert page.evaluate(wide) == width
    page.get_by_role("menuitem", name=re.compile("^Abort run")).click()
    dialog = page.get_by_role("dialog", name=re.compile("^Abort Brief then research"))
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("cannot be resumed")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    box = dialog.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, box
    assert page.evaluate(wide) == width
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    assert api.get(f"/runs/{run}").json()["status"] == "waiting_gate"

    page.get_by_role("button", name="Run actions").click()
    page.get_by_role("menuitem", name=re.compile("^Abort run")).click()
    dialog.get_by_role("button", name="Abort run").click()
    expect(page.get_by_test_id("status-line")).to_have_text(re.compile("^Aborted"))
    assert api.get(f"/runs/{run}").json()["status"] == "aborted"


def test_each_run_state_reads_from_the_first_screen_and_no_markdown_is_raw(
    page: Page, every: dict[str, str], locked_url: str
) -> None:
    """Each run state names itself in its pill on the first screen; a running run's clock ticks;
    a failed one opens on its error with the ways on; a done one on its summary; and no run page
    or reader shows a markdown table as raw pipes."""
    height = page.viewport_size["height"]  # type: ignore[index]
    states = {
        "running": "running",
        "run": "needs you",
        "paused": "paused",
        "failed": "failed",
        "done": "done",
    }
    for key, label in states.items():
        page.goto(f"/worlds/{every['world']}/runs/{every[key]}")
        settle(page)
        pill = page.get_by_test_id("run-pill")
        expect(pill).to_have_text(re.compile(f"^{label}$", re.I))
        box = pill.bounding_box()
        assert box and box["y"] + box["height"] < height, (key, box)
        assert "| Hook |" not in page.locator("main").inner_text(), key
        if key == "running":
            clock = page.get_by_test_id("elapsed").first
            was = clock.inner_text()
            expect(clock).not_to_have_text(was, timeout=3000)
        if key == "failed":
            failure = page.get_by_test_id("failure")
            expect(failure).to_be_in_viewport()
            expect(failure.get_by_role("button", name="Retry from this stage")).to_be_visible()
        if key == "done":
            expect(page.get_by_test_id("done-row")).to_be_in_viewport()
    research = signed_in(locked_url).get(f"/runs/{every['done']}").json()["stages"][0]["id"]
    for route in (
        f"/worlds/{every['world']}/runs/{every['done']}?stage={research}",
        f"/worlds/{every['world']}/read?path={every['doc']}",
    ):
        page.goto(route)
        settle(page)
        expect(page.locator("main table").first).to_be_visible()
        assert "| Hook |" not in page.locator("main").inner_text(), route
