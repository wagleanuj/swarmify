from playwright.sync_api import Page, expect

from swarmify_api.main import VERSION


def test_home_page_shows_api_version(page: Page, base_url: str) -> None:
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Swarmify")).to_be_visible()
    expect(page.get_by_test_id("version")).to_have_text(VERSION)
