"""The golden path, through the UI only where a person would act: create a world, have Richard
draft the workflow, run it through a pick gate, a render, a review gate and a publish confirm
gate, then find the artifacts on the timeline and in the library and the files on disk; then
upkeep writes the views back and retention prunes the video.

Behind the API: the scripted agent CLI (no model) and the fake render and YouTube connectors."""

import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

KEYS = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}


def test_draft_with_the_planner_run_every_gate_and_publish(
    page: Page, base_url: str, data_dir: Path, until: Callable[..., None]
) -> None:
    page.goto(base_url)
    page.get_by_label("Name").fill("Golden")
    page.get_by_role("button", name="Create world").click()
    expect(page.get_by_role("heading", name="Golden", level=1)).to_be_visible()
    world = page.url.rstrip("/").split("/")[-1]
    try:
        _run(page, base_url, data_dir, world, until)
    finally:
        httpx.delete(f"{base_url}/api/worlds/{world}")


def _run(page: Page, base_url: str, data_dir: Path, world: str, until: Callable[..., None]) -> None:
    httpx.put(
        f"{base_url}/api/connectors/builtin_youtube/credentials", json={"credentials": KEYS}
    ).raise_for_status()

    # Richard drafts it from plain words; the stage rail and the check follow the draft.
    page.get_by_role("link", name="New workflow").click()
    page.get_by_role("tab", name="Richard").click()
    page.get_by_label("Message to Richard").fill(
        "A weekly short: research ideas, I pick one, render it, I review it, then post it."
    )
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_test_id("chat-log")).to_contain_text("post after you confirm")
    expect(page.get_by_test_id("check-status")).to_contain_text("Richard updated the draft")
    rail = page.get_by_test_id("stage-rail")
    for name in ("research", "pick", "render", "review", "confirm", "publish"):
        expect(rail).to_contain_text(name)
    expect(page.get_by_test_id("problems")).to_contain_text("No problems")
    page.get_by_role("button", name="Save as version 1").click()
    expect(page.get_by_test_id("versions")).to_contain_text("v1")

    page.get_by_role("link", name="golden").click()
    page.get_by_role("button", name="Start a run of Weekly short").click()
    status = page.get_by_test_id("status-line")
    expect(status).to_have_text("Waiting for you at stage 2: pick.", timeout=20_000)
    run = page.url.rstrip("/").split("/")[-1]
    cards = page.get_by_test_id("artifact-card")
    expect(cards.first).to_contain_text("#1 of 2")
    expect(cards.first).to_contain_text("Hook first")

    # Pick, then the render runs and the review gate opens in the same inbox card.
    page.get_by_role("link", name="Answer the gate").click()
    card = page.get_by_test_id(f"gate-{run}")
    expect(card).to_contain_text("Pick one idea.")
    card.get_by_label("Your pick").fill("Hook first")
    card.get_by_role("button", name="Continue with this pick").click()
    expect(card).to_contain_text("Watch it. Approve or send back.", timeout=20_000)
    card.get_by_role("button", name="Approve").click()

    # The confirm gate shows exactly what goes out, and only then posts it.
    expect(card).to_contain_text("Post it?", timeout=20_000)
    out = card.get_by_label("What goes out")
    expect(out).to_contain_text("Five hooks that work")
    expect(out).to_contain_text("unlisted")
    expect(out).to_contain_text("short.mp4")
    expect(out).to_contain_text("Now, as soon as you confirm")
    card.get_by_role("button", name="Confirm and post").click()
    expect(page.get_by_test_id("no-gates")).to_be_visible(timeout=20_000)

    page.goto(f"{base_url}/worlds/{world}/runs/{run}")
    expect(status).to_have_text("Done. All 7 stages finished.", timeout=20_000)
    timeline = page.get_by_test_id("timeline")
    expect(timeline).to_contain_text("You picked Hook first.")
    expect(timeline).to_contain_text("You approved.")
    expect(timeline).to_contain_text("You confirmed.")
    expect(page.get_by_test_id("artifact-card").first).to_contain_text("Hook first")
    timeline.locator("li", has_text="render").get_by_role("button", name="Show the call").click()
    expect(page.get_by_test_id("call")).to_contain_text("Out: short.mp4")
    timeline.locator("li", has_text="publish").get_by_role("button", name="Show the call").click()
    expect(page.get_by_test_id("call-response")).to_contain_text("https://youtu.be/e2e-video")

    page.goto(f"{base_url}/worlds/{world}/library")
    library = page.get_by_test_id("library")
    expect(library).to_contain_text("Hook first")
    expect(library.get_by_test_id("artifact").filter(has_text="video")).not_to_have_count(0)
    expect(library.get_by_test_id("artifact").filter(has_text="publish")).not_to_have_count(0)

    folder = data_dir / "worlds" / world / "runs" / run
    files = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    # Every decision is a markdown file in its gate's stage folder (stories 57 and 60).
    decisions = [p.read_text() for p in folder.rglob("decision.md")]
    assert len(decisions) == 3
    assert any("Hook first" in d for d in decisions)
    assert any(f.endswith("artifacts/idea-hook-first.md") for f in files)
    assert any(f.endswith("artifacts/short.mp4") for f in files)
    assert any(f.endswith("artifacts/short.mp4.md") for f in files)
    (publish,) = folder.rglob("publish.md")
    assert "https://youtu.be/e2e-video" in publish.read_text()

    # Upkeep writes the video's views into the publish record and onto the scorecard (84, 85).
    until(lambda: "1,234 views" in publish.read_text())
    assert "e2e-video: 1,234 views" in publish.read_text()  # in the body, where the graph reads
    page.goto(f"{base_url}/worlds/{world}")
    page.get_by_role("link", name="Scorecard of Weekly short").click()
    expect(page.get_by_test_id("score")).to_contain_text("1,234 views")

    # A hundred days on, retention deletes the video and keeps its sidecar (story 68).
    (video,) = folder.rglob("short.mp4")
    old = time.time() - 100 * 86400
    os.utime(video, (old, old))
    until(lambda: not video.exists())
    assert not video.exists()
    assert "deleted by retention" in video.with_name("short.mp4.md").read_text()
