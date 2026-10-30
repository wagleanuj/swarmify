import re
import shlex
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

ECHO = Path(__file__).parents[2] / "packages/connectors/tests/echo_server.py"
RENDER = """name: Render then pick
stages:
  - connector: render
    calls: echo
    output: short.mp4
    params: {tool: render}
  - gate: pick
    ask: Ship it?
"""


def test_add_a_connector_see_its_tools_and_open_its_call_on_the_timeline(
    page: Page, base_url: str, tmp_path: Path
) -> None:
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Video"}).json()
    try:
        page.goto(f"{base_url}/worlds/{world['id']}")
        page.get_by_role("navigation", name="Main").get_by_role("link", name="Connectors").click()
        expect(page.get_by_test_id("connector-list")).to_contain_text("YouTube")
        expect(page.get_by_test_id("connector-list")).not_to_contain_text("Web search")
        page.get_by_role("button", name="Add a connector").click()

        page.get_by_role("textbox", name="Name", exact=True).fill("echo")
        command = shlex.join([sys.executable, str(ECHO), str(tmp_path)])
        page.get_by_role("textbox", name="MCP server").fill(command)
        page.get_by_role("textbox", name="Keys").fill("ECHO_KEY=e2e-secret")
        page.get_by_role("button", name="Test and list tools").click()
        tools = page.get_by_test_id("tools")
        expect(tools).to_contain_text("offers 5 tools", timeout=15_000)
        expect(tools).to_contain_text("render_status")
        page.get_by_role("button", name="Save connector").click()
        expect(page.locator("[data-name=echo]")).to_contain_text("Ready")

        created = httpx.post(
            f"{base_url}/api/worlds/{world['id']}/workflows", json={"yaml": RENDER}
        ).json()
        run = httpx.post(f"{base_url}/api/workflows/{created['workflow_id']}/runs", json={}).json()
        page.goto(f"{base_url}/worlds/{world['id']}/runs/{run['id']}")
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text("Waiting for you: Ship it?", timeout=20_000)

        page.get_by_test_id("timeline").get_by_role("button", name=re.compile(r"^Step 1,")).click()
        page.get_by_text("Technical details").click()
        expect(page.get_by_test_id("call")).to_contain_text("polled 2 times")
        request = page.get_by_test_id("call-request")
        expect(request).to_contain_text(f'"idempotency_key": "{run["stages"][0]["id"]}"')
        expect(page.get_by_test_id("call-response")).to_contain_text('"output": "short.mp4"')
        expect(page.get_by_test_id("call")).not_to_contain_text("e2e-secret")
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")


SAY = """name: Say it
stages:
  - connector: say
    calls: echo
    output: said.wav
"""


def test_a_world_enables_an_installed_package_and_disables_it(page: Page, base_url: str) -> None:
    source = "packages/connectors/tests/fixtures/packages/echo"
    installed = httpx.post(f"{base_url}/api/packages", json={"source": source})
    assert installed.status_code == 201, installed.text
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Packages"}).json()
    other = httpx.post(f"{base_url}/api/worlds", json={"name": "Other"}).json()
    workflows = f"{base_url}/api/worlds/{world['id']}/workflows"
    try:
        httpx.put(f"{base_url}/api/worlds/{other['id']}/packages/echo", json={"version": "1.0.0"})
        page.goto(f"{base_url}/worlds/{world['id']}/connectors")
        row = page.locator("[data-package=echo]")
        expect(row).to_contain_text("echo 1.0.0")
        expect(row).to_contain_text("Installed")
        expect(page.get_by_test_id("connector-list")).to_contain_text("YouTube")
        assert httpx.post(workflows, json={"yaml": SAY}).status_code == 422

        row.get_by_role("button", name="Enable echo 1.0.0").click()
        expect(row).to_contain_text("Enabled here")
        expect(page.locator("[data-name=echo]")).to_contain_text("package echo 1.0.0")
        assert httpx.post(workflows, json={"yaml": SAY}).status_code == 201

        page.once("dialog", lambda d: d.accept())
        row.get_by_role("button", name="Disable echo 1.0.0").click()
        expect(row.get_by_role("button", name="Enable echo 1.0.0")).to_be_visible()
        expect(page.locator("[data-name=echo]")).to_have_count(0)
        enabled = httpx.get(f"{base_url}/api/worlds/{other['id']}/packages").json()
        assert enabled == [{"name": "echo", "version": "1.0.0"}]
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
        httpx.delete(f"{base_url}/api/worlds/{other['id']}")
