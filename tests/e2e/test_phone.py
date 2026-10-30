"""Every page at phone width, signed in, as an operator answering a gate from the sofa.

A page passes when it renders its content with no sideways scroll: nothing wider than the
screen. Runs wait at a gate (one early in a long workflow), run and finish with a video, so the
timeline, gates inbox, agent log and reader all have content (issue 182)."""

import io
import re
import time
import wave
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

from scripted_app import LONG

PASSWORD = "aviato"  # what the `locked_url` app expects
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
RENDER = """name: Research then render
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
    produces: [ideas.md]
  - connector: render
    calls: remotion_render
    input: ideas.md
    output: short.mp4
    params: {project: /tmp, composition: Short}
"""
CHILD = "name: Brief\nstages:\n  - gate: brief\n    ask: Go?\n"
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
    "/worlds/{world}/runs/{long}",
    "/worlds/{world}/runs/{running}",
    "/worlds/{world}/runs/{done}",
    "/worlds/{world}/read?path={doc}",
    "/worlds/{world}/connectors",
    "/worlds/{world}/graph",
    "/worlds/{world}/library",
    "/worlds/{world}/assets",
    "/worlds/{world}/cast",
    "/worlds/{world}/settings",
    "/evals",
]


def signed_in(url: str) -> httpx.Client:
    """An API client signed in as the operator; it keeps the session cookie."""
    api = httpx.Client(base_url=f"{url}/api")
    api.post("/session", json={"password": PASSWORD}).raise_for_status()
    return api


@pytest.fixture(scope="module")
def ids(locked_url: str) -> dict[str, str]:
    """A run in every state: `run` and `long` wait at a gate, `running` waits on its child run,
    `done` rendered a video; `doc` is the ideas.md `done`'s research made."""
    api = signed_in(locked_url)
    world = api.post("/worlds", json={"name": "Video"}).json()["id"]
    api.patch(f"/worlds/{world}", json={"run_cap": 8}).raise_for_status()  # the default 2 queues

    def make(yaml: str) -> str:
        made = api.post(f"/worlds/{world}/workflows", json={"yaml": yaml})
        assert made.status_code == 201, made.text
        return made.json()["workflow_id"]

    def start(workflow: str, status: str) -> str:
        # One at a time: the scripted swarm's sessions share one script, so two at once race.
        run = api.post(f"/workflows/{workflow}/runs", json={}).json()["id"]
        for _ in range(200):
            if api.get(f"/runs/{run}").json()["status"] == status:
                return run
            time.sleep(0.1)
        raise AssertionError(f"{workflow}'s run never reached {status}")

    workflow = make(FLOW)
    run = start(workflow, "waiting_gate")
    long = start(make(LONG), "waiting_gate")
    done = start(make(RENDER), "done")
    parent = f"name: Parent\nstages:\n  - workflow: child\n    runs: {make(CHILD)}\n"
    running = start(make(parent), "running")
    files = [a["path"] for a in api.get(f"/runs/{done}/artifacts").json()]
    doc = next(p for p in files if p.endswith("/ideas.md"))
    gates = {g["run_id"]: g["stage_id"] for g in api.get("/gates").json()}
    return {
        "world": world,
        "workflow": workflow,
        "run": run,
        "long": long,
        "running": running,
        "done": done,
        "doc": doc,
        "run_gate": gates[run],
        "long_gate": gates[long],
    }


@pytest.fixture
def phone(browser: Browser, locked_url: str) -> Iterator[Page]:
    context = browser.new_context(
        viewport=PHONE, base_url=locked_url, is_mobile=True, has_touch=True
    )
    context.request.post("/api/session", data={"password": PASSWORD})
    yield context.new_page()
    context.close()  # an open event stream would hold the server's shutdown


def test_signing_in_opens_the_app_until_signing_out(
    browser: Browser, locked_url: str, ids: dict
) -> None:
    """Issue 227: the sign-in page, a wrong then the right password, staying signed in across
    a reload, and signing out from Settings."""
    assert httpx.get(f"{locked_url}/api/worlds").status_code == 401
    context = browser.new_context(viewport=PHONE, base_url=locked_url, is_mobile=True)
    page = context.new_page()
    page.goto("/gates")
    password = page.get_by_label("Password")
    password.fill("hooli")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("That is not the password")
    password.fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Needs you")).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Needs you")).to_be_visible()

    page.goto(f"/worlds/{ids['world']}/settings")
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_label("Password")).to_be_visible()
    page.reload()
    expect(page.get_by_label("Password")).to_be_visible()
    context.close()


def _fits(page: Page) -> None:
    """No sideways scroll, and the layout viewport is still the phone's 390 px."""
    assert page.evaluate("window.innerWidth") == PHONE["width"]  # the viewport meta holds
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    culprits = page.evaluate(
        """[...document.querySelectorAll('body *')]
            .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
            .slice(0, 5).map(e => e.tagName + '.' + e.className)"""
    )
    assert wide <= 0, f"{page.url} scrolls sideways by {wide}px: {culprits}"


@pytest.mark.parametrize("path", PAGES)
def test_every_page_fits_a_phone(phone: Page, ids: dict[str, str], path: str) -> None:
    phone.goto(path.format(**ids))
    # Event streams keep the network busy, so wait for the page's heading, not for idle.
    expect(phone.locator("h1").first).to_be_visible()
    expect(phone.get_by_text("Loading...")).to_have_count(0)
    _fits(phone)
    # The sidebar gives way to a bottom tab bar; every tab and More's rows are a 44px thumb tap.
    expect(phone.get_by_role("navigation", name="Main")).to_be_hidden()
    tabs = phone.get_by_role("navigation", name="Tabs")
    heights = tabs.locator(".tabbar-item").evaluate_all("as => as.map(a => a.offsetHeight)")
    assert len(heights) == 5 and all(h >= 44 for h in heights), f"{path} has tabs {heights}px tall"
    tabs.get_by_role("button", name="More").click()
    rows = phone.locator(".sheet-row").evaluate_all("as => as.map(a => a.offsetHeight)")
    assert rows and all(h >= 44 for h in rows), f"{path} has More rows {rows}px tall"


@pytest.mark.parametrize("where", ["gates", "run"])
def test_the_answer_is_on_screen_above_the_tab_bar(phone: Page, ids: dict, where: str) -> None:
    """Waiting at step 2 of 9: Approve is in view on load, no scrolling, clear of the tab bar."""
    phone.goto(
        f"/gates?gate={ids['long_gate']}"
        if where == "gates"
        else f"/worlds/{ids['world']}/runs/{ids['long']}"
    )
    card = phone.get_by_test_id(f"gate-{ids['long']}")
    expect(card).to_contain_text("Good enough to go on?")
    approve = card.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_in_viewport(ratio=1)
    expect(card.get_by_role("button", name="Send back to...")).to_be_in_viewport(ratio=1)
    assert phone.evaluate("window.scrollY") == 0
    box, bar = approve.bounding_box(), phone.locator(".tabbar").bounding_box()
    assert box and bar and box["y"] + box["height"] <= bar["y"], (box, bar)
    if where == "gates":  # several wait, so decision chips lead the page
        expect(phone.get_by_test_id("queue").locator(".q-item")).to_have_count(3)
    else:  # seven steps not started still fit the strip (issue 175)
        expect(phone.get_by_test_id("strip")).to_be_visible()
        assert phone.get_by_test_id("strip").bounding_box()["width"] <= PHONE["width"]  # type: ignore[index]
    _fits(phone)


def test_a_running_run_and_a_done_run_with_its_video(phone: Page, ids: dict) -> None:
    phone.goto(f"/worlds/{ids['world']}/runs/{ids['running']}")
    expect(phone.get_by_test_id("run-pill")).to_have_text(re.compile("^running$", re.I))
    expect(phone.get_by_role("link", name="‹ Runs")).to_be_visible()
    _fits(phone)
    phone.goto(f"/worlds/{ids['world']}/runs/{ids['done']}")
    expect(phone.get_by_test_id("run-pill")).to_have_text(re.compile("^done$", re.I))
    player = phone.get_by_test_id("result").get_by_test_id("player")
    expect(player).to_be_visible()
    assert player.bounding_box()["height"] <= 300  # type: ignore[index]
    phone.get_by_role("button", name="All steps").click()
    sheet = phone.get_by_role("dialog", name="All steps")
    expect(sheet.get_by_test_id("timeline").locator("li")).to_have_count(3)  # and the retro
    _fits(phone)


def test_a_document_opens_from_its_run_into_the_reader(phone: Page, ids: dict) -> None:
    research = ids["doc"].split("/")[2]
    phone.goto(f"/worlds/{ids['world']}/runs/{ids['done']}?stage={research}")
    panel = phone.get_by_test_id("stage-panel")
    panel.get_by_test_id("all-files").locator("summary").click()
    panel.get_by_test_id("all-files").get_by_role("link", name="ideas.md").click()
    expect(phone).to_have_url(re.compile(r"/read\?path=.*ideas\.md"))
    doc = phone.get_by_test_id("document")
    expect(doc.locator("pre")).to_contain_text("ffmpeg")
    _fits(phone)
    # The code block and the table scroll inside themselves; the page stays put.
    for wide in (doc.locator("pre"), doc.locator(".scroll-x")):
        sizes = wide.evaluate("e => [e.scrollWidth, e.clientWidth]")
        assert sizes[0] > sizes[1], f"{wide} is not wider than its box: {sizes}"
    raw = phone.get_by_role("link", name="Raw file").get_attribute("href")
    response = phone.request.get(raw)  # type: ignore[arg-type]
    assert response.status == 200 and response.text().endswith("1. Hooks")


def test_a_gate_is_answered_from_a_phone_as_the_stream_moves(phone: Page, ids: dict) -> None:
    phone.goto(f"/worlds/{ids['world']}/runs/{ids['run']}")
    status = phone.get_by_test_id("status-line")
    expect(status).to_have_text("Waiting for you: Which idea do we shoot?", timeout=15_000)
    phone.goto(f"/gates?gate={ids['run_gate']}")
    card = phone.get_by_test_id(f"gate-{ids['run']}")
    expect(card).to_contain_text("Which idea do we shoot?")
    card.get_by_role("button", name="Approve").click()
    expect(phone.get_by_test_id("answered")).to_contain_text("You approved it", timeout=15_000)
    expect(card).to_have_count(0)  # the next decision opens in its place


def _clip(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return buf.getvalue()


def test_assets_plays_a_voice_first_and_pins_add_a_voice_above_the_tab_bar(
    phone: Page, ids: dict, locked_url: str
) -> None:
    """Issue 194: a voice added over the API is a playable card; Add a voice opens its sheet."""
    api = signed_in(locked_url)
    form = {"name": "narrator", "description": "Warm and calm", "consent": "Mine."}
    files = {"clip": ("n.wav", _clip(6), "audio/wav")}
    added = api.post(f"/worlds/{ids['world']}/voices", data=form, files=files)
    assert added.status_code in (201, 422), added.text  # 422: a rerun on the same world
    api.put(f"/worlds/{ids['world']}/assets/logo.png", content=b"PNG")
    phone.goto(f"/worlds/{ids['world']}/assets")
    card = phone.get_by_role("listitem", name="narrator")
    expect(card.get_by_role("button", name="Play narrator")).to_be_visible()
    expect(card).to_contain_text("6.0 s clip")
    expect(phone.get_by_role("listitem", name="logo.png")).to_contain_text("KB")
    add = phone.get_by_role("button", name="Add a voice")
    expect(add).to_be_in_viewport(ratio=1)
    box, bar = add.bounding_box(), phone.locator(".tabbar").bounding_box()
    assert box and bar and box["y"] + box["height"] <= bar["y"], (box, bar)
    _fits(phone)
    add.click()
    sheet = phone.get_by_role("dialog", name="Add a voice")
    expect(sheet.get_by_role("button", name="Record")).to_be_visible()
    expect(
        sheet.get_by_text("This is my voice, or I have the speaker's permission")
    ).to_be_visible()
    _fits(phone)
