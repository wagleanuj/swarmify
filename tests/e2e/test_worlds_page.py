from pathlib import Path

from playwright.sync_api import Page, expect


def test_create_open_and_delete_a_world(page: Page, base_url: str, data_dir: Path) -> None:
    page.goto(base_url)
    expect(page.get_by_test_id("empty")).to_be_visible()

    page.get_by_label("Name").fill("Video")
    page.get_by_label("What is this world about?").fill("Weekly shorts for the attraction channel.")
    page.get_by_role("button", name="Create world").click()

    expect(page.get_by_role("heading", name="Video", level=1)).to_be_visible()
    expect(page.get_by_text("Weekly shorts for the attraction channel.")).to_be_visible()

    page.get_by_role("link", name="All worlds").click()
    expect(page.get_by_role("main").get_by_role("link", name="Video")).to_be_visible()
    expect(page.get_by_test_id("empty")).not_to_be_visible()

    page.get_by_role("main").get_by_role("link", name="Video").click()
    folder = data_dir / "worlds" / page.url.rstrip("/").split("/")[-1]
    assert folder.is_dir()
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Settings").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete Video").click()
    expect(page.get_by_role("heading", name="Your worlds")).to_be_visible()
    expect(page.get_by_test_id("empty")).to_be_visible()
    assert not folder.exists()  # its runs, files and graph went with it (story 7)
