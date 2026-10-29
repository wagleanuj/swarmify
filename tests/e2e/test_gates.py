import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

ECHO = Path(__file__).parents[2] / "packages/connectors/tests/echo_server.py"
ECHO_CONNECTOR = {
    "name": "echo",
    "kind": "stage",
    "server": shlex.join([sys.executable, str(ECHO)]),
    "stage_types": ["connector"],
}
REVIEW = """name: Review loop
stages:
  - connector: render
    calls: echo
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
"""


def test_answer_a_gate_from_the_inbox_and_watch_the_run_finish(page: Page, base_url: str) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Video"}).json()
    try:
        connectors = f"{base_url}/api/worlds/{world['id']}/connectors"
        httpx.post(connectors, json=ECHO_CONNECTOR).raise_for_status()
        wf = httpx.post(f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": REVIEW})
        run = httpx.post(f"{base_url}/api/workflows/{wf.json()['workflow_id']}/runs", json={})
        page.goto(f"{base_url}/gates")
        card = page.get_by_test_id(f"gate-{run.json()['id']}")
        expect(card).to_contain_text("Watch it. Approve or send back.", timeout=15_000)
        expect(card).to_contain_text("2 of 2 revisions left")

        card.get_by_label(r"Note").fill("Darker.")
        card.get_by_role("button", name="Send back to...").click()
        card.get_by_role("menuitem").first.click()
        expect(card).to_contain_text("1 of 2 revisions left", timeout=15_000)

        card.get_by_role("button", name="Approve").click()
        empty = page.get_by_test_id("no-gates")
        expect(empty).to_contain_text("Nothing is waiting on you", timeout=15_000)

        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run.json()['id']}")
        expect(page.get_by_test_id("status-line")).to_have_text(
            re.compile(r"^Done in .*\. Sent back once\.$"), timeout=15_000
        )
        rail = page.get_by_test_id("timeline")
        rail.get_by_role("button", name=re.compile(r"^Step 1,")).click()
        expect(page.get_by_test_id("stage-panel")).to_contain_text("Sent back with: Darker.")
        rail.get_by_role("button", name=re.compile(r"^Step 2,")).click()
        expect(page.get_by_test_id("decision").last).to_contain_text("You approved")
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")


def test_answer_in_needs_you_opens_the_next_and_links_to_the_answered_step(
    page: Page, base_url: str, until: Callable[[Callable[[], object]], None]
) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Queue"}).json()
    try:
        connectors = f"{base_url}/api/worlds/{world['id']}/connectors"
        httpx.post(connectors, json=ECHO_CONNECTOR).raise_for_status()
        wf = httpx.post(f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": REVIEW})
        runs = f"{base_url}/api/workflows/{wf.json()['workflow_id']}/runs"
        first, second = (httpx.post(runs, json={}).json()["id"] for _ in range(2))

        def gates() -> dict[str, str]:
            return {g["run_id"]: g["stage_id"] for g in httpx.get(f"{base_url}/api/gates").json()}

        until(lambda: {first, second} <= gates().keys())
        gate = gates()[first]
        page.goto(f"{base_url}/gates?world={world['id']}&gate={gate}")
        page.get_by_test_id(f"gate-{first}").get_by_role("button", name="Approve").click()

        expect(page.get_by_test_id(f"gate-{second}")).to_contain_text(
            "Watch it. Approve or send back.", timeout=15_000
        )
        line = page.get_by_test_id("answered")
        expect(line).to_contain_text(re.compile(r"You approved it at .*\."))
        line.get_by_role("link", name="Open that run").click()

        expect(page).to_have_url(f"{base_url}/worlds/{world['id']}/runs/{first}?stage={gate}")
        step = page.get_by_test_id("timeline").get_by_role("button", name=re.compile(r"^Step 2,"))
        expect(step).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_test_id("decision").last).to_contain_text("You approved", timeout=15_000)
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
