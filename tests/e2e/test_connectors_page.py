import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import zlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from swarmify_connectors.fake_oauth import FakeProvider

ECHO = Path(__file__).parents[2] / "packages/connectors/tests/echo_server.py"


def install(base_url: str, source: str) -> httpx.Response:
    """Install a package as the Connectors page does: review it, then approve every permission
    it declares. The review's answer when it refuses."""
    review = httpx.post(f"{base_url}/api/packages/review", json={"source": source})
    if review.status_code != 200:
        return review
    approved = review.json()["permissions"]
    return httpx.post(f"{base_url}/api/packages", json={"source": source, "approved": approved})


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
        # the server's source in the command itself, as its sandbox cannot read the checkout
        source = base64.b64encode(zlib.compress(ECHO.read_bytes(), 9)).decode()
        run = f"import base64, zlib; exec(zlib.decompress(base64.b64decode('{source}')))"
        command = shlex.join([sys.executable, "-c", run, str(tmp_path)])
        page.get_by_role("textbox", name="MCP server").fill(command)
        page.get_by_role("textbox", name="Keys").fill("ECHO_KEY=e2e-secret")
        page.get_by_role("button", name="Test and list tools").click()
        tools = page.get_by_test_id("tools")
        expect(tools).to_contain_text("offers 5 tools", timeout=15_000)
        expect(tools).to_contain_text("render_status")
        page.get_by_role("button", name="Save connector").click()
        added = page.locator("[data-name=echo]")
        expect(added).to_contain_text("Ready")
        expect(added).to_contain_text("package echo 0.0.")  # a local package, enabled here
        here = page.locator("[data-package=echo]").filter(has_text="Enabled here")
        expect(here).to_contain_text("An MCP server added on the Connectors page")
        added.get_by_role("button", name="Set up echo").click()
        form = page.get_by_role("form", name="Setup of echo")
        key = form.get_by_label(re.compile(r"^ECHO_KEY · every world · Credential, set"))
        expect(key).to_be_visible()

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
  - swarm: write
    goal: a line
    done_when: written
    output_type: line
    produces: [line.md]
  - connector: say
    calls: echo
    input: line.md
    output: said.wav
"""


def test_a_world_enables_an_installed_package_and_disables_it(page: Page, base_url: str) -> None:
    source = "packages/connectors/tests/fixtures/packages/echo"
    installed = install(base_url, source)
    assert installed.status_code == 201, installed.text
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Packages"}).json()
    other = httpx.post(f"{base_url}/api/worlds", json={"name": "Other"}).json()
    workflows = f"{base_url}/api/worlds/{world['id']}/workflows"
    try:
        httpx.put(f"{base_url}/api/worlds/{other['id']}/packages/echo", json={"version": "1.0.0"})
        page.goto(f"{base_url}/worlds/{world['id']}/connectors")
        row = page.locator("[data-package=echo]").filter(has_text="echo 1.0.0")
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
        assert {"name": "echo", "version": "1.0.0"} in enabled
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
        httpx.delete(f"{base_url}/api/worlds/{other['id']}")


def test_a_packages_setup_form_tests_before_saving(
    page: Page, base_url: str, data_dir: Path, tmp_path: Path
) -> None:
    probe = Path(__file__).parents[2] / "packages/connectors/tests/fixtures/packages/probe"
    source = tmp_path / "probe"
    shutil.copytree(probe, source)
    toml = source / "swarmify-connector.toml"
    toml.write_text(
        toml.read_text()
        .replace('name = "probe"', 'name = "probe_e2e"')
        .replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
    )
    installed = install(base_url, str(source))
    assert installed.status_code == 201, installed.text
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Setup"}).json()
    try:
        httpx.put(
            f"{base_url}/api/worlds/{world['id']}/packages/probe_e2e", json={"version": "1.0.0"}
        )
        page.goto(f"{base_url}/worlds/{world['id']}/connectors")
        row = page.locator("[data-name=probe_e2e]")
        expect(row).to_contain_text("Needs setup")
        row.get_by_role("button", name="Set up probe_e2e").click()
        form = page.get_by_role("form", name="Setup of Probe")
        key = form.get_by_label(re.compile(r"^API key · every world"))
        key.fill("bad")
        form.get_by_label(re.compile(r"^Voice.*this world")).select_option("high")
        form.get_by_role("button", name="Test and save").click()
        expect(form.get_by_role("alert")).to_have_text(
            "Not saved. The test said: The probe service refused that key.", timeout=15_000
        )
        assert (
            not (data_dir / "setup.json").exists()
            or "probe_e2e" not in (data_dir / "setup.json").read_text()
        )

        key.fill("good")
        form.get_by_role("button", name="Test and save").click()
        expect(row.get_by_role("status")).to_have_text(
            "Setup saved. Signed in as probe.", timeout=15_000
        )
        expect(row).to_contain_text("Ready")
        saved = json.loads((data_dir / "setup.json").read_text())["probe_e2e"]
        assert saved == {"shared": {"api_key": "good"}, "worlds": {world["id"]: {"voice": "high"}}}
        form_now = httpx.get(f"{base_url}/api/worlds/{world['id']}/packages/probe_e2e/setup")
        assert '"good"' not in form_now.text
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")


def test_a_pin_switch_shows_the_manifest_diff_and_uninstall_names_the_users(
    page: Page, base_url: str
) -> None:
    fixtures = "packages/connectors/tests/fixtures/packages"
    for source in (f"{fixtures}/echo", f"{fixtures}/echo_2"):
        installed = install(base_url, source)
        assert installed.status_code == 201 or "already installed" in installed.text
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Upgrades"}).json()
    try:
        httpx.put(f"{base_url}/api/worlds/{world['id']}/packages/echo", json={"version": "1.0.0"})
        page.goto(f"{base_url}/worlds/{world['id']}/connectors")
        newer = page.locator("[data-package=echo]", has_text="echo 2.0.0")
        newer.get_by_role("button", name="Enable echo 2.0.0").click()
        shown = page.get_by_role("region", name="Switch echo to 2.0.0")
        expect(shown).to_contain_text("Switch Echo from 1.0.0 to 2.0.0")
        expect(shown).to_contain_text("Added speed")
        expect(shown).to_contain_text("Added region")
        expect(shown).to_contain_text("Added network: eu.example.org")
        expect(shown.get_by_role("status")).to_have_text(
            "Fill in Region in Echo 2.0.0's Setup first; the switch waits for it."
        )
        expect(shown.get_by_role("button", name="Switch to 2.0.0")).to_be_disabled()

        older = page.locator("[data-package=echo]", has_text="echo 1.0.0")
        page.once("dialog", lambda d: d.accept())
        older.get_by_role("button", name="Uninstall echo 1.0.0").click()
        expect(page.get_by_role("alert")).to_contain_text(
            "echo 1.0.0 is in use: world Upgrades pins it"
        )
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
        httpx.delete(f"{base_url}/api/packages/echo/2.0.0")


def test_an_oauth_field_signs_in_through_the_provider_and_back(
    page: Page, base_url: str, data_dir: Path, tmp_path: Path
) -> None:
    probe = Path(__file__).parents[2] / "packages/connectors/tests/fixtures/packages/probe"
    source = tmp_path / "probe"
    shutil.copytree(probe, source)
    toml = source / "swarmify-connector.toml"
    with FakeProvider() as provider:
        toml.write_text(
            toml.read_text()
            .replace('name = "probe"', 'name = "probe_oauth"')
            .replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
            + f"""
[[config]]
key = "account"
label = "Account"
type = "oauth"
required = true
scope = "world"
env = "PROBE_TOKEN"
[config.oauth]
authorize_url = "{provider.url}/authorize"
token_url = "{provider.url}/token"
scopes = ["read"]
pkce = true
client = "operator"
"""
        )
        installed = install(base_url, str(source))
        assert installed.status_code == 201, installed.text
        world = httpx.post(f"{base_url}/api/worlds", json={"name": "Sign-in"}).json()
        try:
            httpx.put(
                f"{base_url}/api/worlds/{world['id']}/packages/probe_oauth",
                json={"version": "1.0.0"},
            )
            page.goto(f"{base_url}/worlds/{world['id']}/connectors")
            row = page.locator("[data-name=probe_oauth]")
            expect(row).to_contain_text("Needs setup")
            row.get_by_role("button", name="Set up probe_oauth").click()
            form = page.get_by_role("form", name="Setup of Probe")
            expect(form).to_contain_text(f"{base_url}/api/setup/oauth/callback")
            form.get_by_label(re.compile(r"^API key · every world")).fill("good")
            form.get_by_label(re.compile(r"^Account client ID.*every world")).fill("app")
            form.get_by_label(re.compile(r"^Account client secret.*every world")).fill("shh")
            form.get_by_role("button", name="Test and save").click()
            expect(row.get_by_role("status")).to_contain_text("Setup saved.", timeout=15_000)

            row.get_by_role("button", name="Set up probe_oauth").click()
            form.get_by_role("button", name="Sign in to Account").click()
            # to the provider, which consents at once, and back through the callback
            expect(page.get_by_role("status")).to_have_text(
                "Signed in. probe_oauth gets a fresh access token each time it starts.",
                timeout=15_000,
            )
            expect(page).to_have_url(f"{base_url}/worlds/{world['id']}/connectors")
            expect(row).to_contain_text("Ready")
            saved = json.loads((data_dir / "setup.json").read_text())["probe_oauth"]
            tokens = saved["worlds"][world["id"]]["account"]
            assert tokens["access_token"] in provider.access_tokens
            form_now = httpx.get(
                f"{base_url}/api/worlds/{world['id']}/packages/probe_oauth/setup"
            ).text
            assert tokens["access_token"] not in form_now and "shh" not in form_now
        finally:
            httpx.delete(f"{base_url}/api/worlds/{world['id']}")


def git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(["git", *who, *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_install_from_git_shows_the_permissions_and_asks_again_for_an_upgrade(
    page: Page, base_url: str, tmp_path: Path
) -> None:
    work, bare = tmp_path / "work", tmp_path / "echo_git.git"
    shutil.copytree(ECHO.parent / "fixtures/packages/echo", work)
    toml = work / "swarmify-connector.toml"
    toml.write_text(toml.read_text().replace('name = "echo"', 'name = "echo_git"'))
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "1.0.0")
    git(work, "tag", "v1.0.0")
    toml.write_text(
        toml.read_text()
        .replace('version = "1.0.0"', 'version = "2.0.0"')
        .replace('network = ["api.example.org"]', 'network = ["api.example.org", "eu.example.org"]')
    )
    git(work, "commit", "-qam", "2.0.0")
    git(work, "tag", "v2.0.0")
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    world = httpx.post(f"{base_url}/api/worlds", json={"name": "Git"}).json()
    try:
        page.goto(f"{base_url}/worlds/{world['id']}/connectors")
        page.get_by_role("button", name="Install a package").click()
        form = page.get_by_role("form", name="Install a package")
        form.get_by_role("textbox", name="Git URL or folder").fill(str(bare))
        ref = form.get_by_role("textbox", name=re.compile("Tag, branch or commit"))
        ref.fill("v1.0.0")
        form.get_by_role("button", name="Review permissions").click()
        shown = form.get_by_role("region", name="Permissions of echo_git 1.0.0")
        expect(shown).to_contain_text("Echo 1.0.0 asks for", timeout=15_000)
        expect(shown).to_contain_text("api.example.org")
        expect(shown).to_contain_text("ECHO_KEY")
        expect(shown).to_contain_text("auth.example.org")
        names = [p["name"] for p in httpx.get(f"{base_url}/api/packages").json()]
        assert "echo_git" not in names  # nothing installed, nothing ran
        shown.get_by_role("button", name="Approve and install 1.0.0").click()
        expect(page.get_by_text("Installed Echo 1.0.0.")).to_be_visible(timeout=30_000)
        row = page.locator("[data-package=echo_git]").filter(has_text="echo_git 1.0.0")
        expect(row).to_contain_text("Installed")

        page.get_by_role("button", name="Install a package").click()
        form.get_by_role("textbox", name="Git URL or folder").fill(str(bare))
        ref.fill("v2.0.0")
        form.get_by_role("button", name="Review permissions").click()
        upgrade = form.get_by_role("region", name="Permissions of echo_git 2.0.0")
        expect(upgrade.get_by_role("status")).to_have_text(
            "New since 1.0.0: network: eu.example.org. Installing 2.0.0 grants it too.",
            timeout=15_000,
        )
        upgrade.get_by_role("button", name="Approve and install 2.0.0").click()
        newer_row = page.locator("[data-package=echo_git]").filter(has_text="echo_git 2.0.0")
        expect(newer_row).to_be_visible(timeout=30_000)
        listed = httpx.get(f"{base_url}/api/packages").json()
        (newer,) = [p for p in listed if p["name"] == "echo_git" and p["version"] == "2.0.0"]
        assert newer["ref"] == "v2.0.0" and len(newer["commit"]) == 40
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world['id']}")
        for version in ("1.0.0", "2.0.0"):
            httpx.delete(f"{base_url}/api/packages/echo_git/{version}")


def test_the_index_is_browsed_and_an_entry_installs_through_the_review(
    page: Page, serve: Callable[..., AbstractContextManager[str]], tmp_path: Path
) -> None:
    work, bare = tmp_path / "mono", tmp_path / "mono.git"
    package = work / "connectors/echo_index"
    shutil.copytree(ECHO.parent / "fixtures/packages/echo", package)
    toml = package / "swarmify-connector.toml"
    toml.write_text(toml.read_text().replace('name = "echo"', 'name = "echo_index"'))
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "1.0.0")
    git(work, "tag", "v1.0.0")
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    index = tmp_path / "index.toml"
    index.write_text(
        f"""[[packages]]
name = "echo_index"
description = "Says back what it is given."
git = "{bare}"
subfolder = "connectors/echo_index"
ref = "v1.0.0"
permissions = ["network: api.example.org"]

[[packages]]
name = "other"
description = "Something else."
git = "https://example.org/other.git"
ref = "v1.0.0"
permissions = []
"""
    )
    with serve(tmp_path / "data", SWARMIFY_CONNECTOR_INDEX=str(index)) as url:
        world = httpx.post(f"{url}/api/worlds", json={"name": "Index"}).json()
        page.goto(f"{url}/worlds/{world['id']}/connectors")
        page.get_by_role("button", name="Browse the index").click()
        browse = page.get_by_role("region", name="Browse the index")
        rows = browse.get_by_role("list", name="Packages in the index").get_by_role("listitem")
        expect(rows).to_have_count(2)
        browse.get_by_role("searchbox", name="Search").fill("says back")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("Asks for: network: api.example.org")
        rows.get_by_role("button", name="Review and install echo_index").click()

        form = page.get_by_role("form", name="Install a package")
        shown = form.get_by_role("region", name="Permissions of echo_index 1.0.0")
        expect(shown).to_contain_text("ECHO_KEY", timeout=15_000)  # the clone's, not the index's
        shown.get_by_role("button", name="Approve and install 1.0.0").click()
        expect(page.get_by_text("Installed Echo 1.0.0.")).to_be_visible(timeout=30_000)
        installed = {p["name"]: p for p in httpx.get(f"{url}/api/packages").json()}
        assert installed["echo_index"]["subfolder"] == "connectors/echo_index"
