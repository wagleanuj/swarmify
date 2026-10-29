"""Learning as the operator sees it: the retro's one skill proposal at a gate, edited and
approved; the world's skills beside a shared one on the cast page; a cast member's record and a
confirmed prompt edit with its history; a library idea made into a skill; and the workflow's
scorecard. A server of its own whose retro proposes a skill on every run."""

import re
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

RESEARCH = """name: Research
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
    output_type: list[idea]
"""
EDITED = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nThe hook in the first second.\n"
SHARED = "---\nname: taste\ndescription: My taste, for every world.\n---\n\nNo clickbait.\n"


def test_approve_an_edited_skill_proposal_and_see_skills_records_and_scores(
    page: Page, serve: Any, tmp_path: Path
) -> None:
    (tmp_path / "skills/taste").mkdir(parents=True)
    (tmp_path / "skills/taste/SKILL.md").write_text(SHARED)
    with serve(tmp_path, SWARMIFY_E2E_RETRO="propose") as base_url:
        api = httpx.Client(base_url=f"{base_url}/api")
        world = api.post("/worlds", json={"name": "Video"}).json()["id"]
        workflow = api.post(f"/worlds/{world}/workflows", json={"yaml": RESEARCH}).json()

        page.goto(f"{base_url}/worlds/{world}")
        page.get_by_role("button", name="Start a run of Research").click()
        status = page.get_by_test_id("status-line")
        expect(status).to_have_text(
            "Waiting for you: The retro read this run and proposes a change."
            " Approve it, edit it, or reject it.",
            timeout=20_000,
        )
        run = page.url.split("?")[0].rstrip("/").split("/")[-1]

        # The page may still be on the step it opened at; choosing the retro opens its answer.
        page.get_by_role("button", name=re.compile(r"^Step 2, retro,")).click()
        card = page.get_by_test_id(f"gate-{run}")
        expect(card).to_have_attribute("data-kind", "skill")
        change = card.get_by_test_id("skill-change")
        expect(change).to_contain_text("New skill hooks")
        expect(change).to_contain_text("the judge passed the hook-first idea as #1")
        change.get_by_label("SKILL.md (edit before approving)").fill(EDITED)
        card.get_by_role("button", name="Approve").click()
        expect(status).to_have_text(re.compile(r"^Done in "), timeout=20_000)
        skill = tmp_path / "worlds" / world / "skills/hooks/SKILL.md"
        assert skill.read_text() == EDITED
        page.get_by_role("button", name=re.compile(r"^Step 2, retro,")).click()
        expect(page.get_by_test_id("retro")).to_contain_text("You approved it.")

        # The cast: skills by source, records, and a prompt edit behind a confirmation.
        page.goto(f"{base_url}/worlds/{world}/cast")
        expect(page.get_by_test_id("skill-hooks")).to_contain_text("this world")
        expect(page.get_by_test_id("skill-hooks")).to_contain_text("v1 · from run")
        expect(page.get_by_test_id("skill-taste")).to_contain_text("shared")
        gilfoyle = page.get_by_test_id("cast-worker-1")
        # Tasks, judged, first try, send backs, cost
        expect(gilfoyle.locator("tr").first.locator("td.num")).to_have_text(
            ["1", "1", "1 of 1", "0", re.compile(r"^\$")]
        )
        expect(page.get_by_test_id("cast").locator("tbody.cast-member")).to_have_count(6)
        gilfoyle.get_by_role("button", name="Edit Gilfoyle").click()
        gilfoyle.get_by_label("Standing instructions").fill("Write hooks first.")
        page.once("dialog", lambda dialog: dialog.accept())
        gilfoyle.get_by_role("button", name="Save as version 2").click()
        expect(page.get_by_text("Saved version 2 of Gilfoyle.")).to_be_visible()
        gilfoyle.get_by_role("button", name="History of Gilfoyle").click()
        expect(gilfoyle.get_by_test_id("history")).to_contain_text("Write hooks first.")

        # A library idea becomes a skill draft the operator saves.
        page.goto(f"{base_url}/worlds/{world}/library")
        page.get_by_test_id("artifact").filter(has_text="Hook first").first.click()
        preview = page.get_by_test_id("preview")
        preview.get_by_role("button", name="Make a skill from Hook first").click()
        preview.get_by_role("button", name="Save skill").click()
        expect(page.get_by_text("Saved hook-first as version 1.")).to_be_visible()
        assert (tmp_path / "worlds" / world / "skills/hook-first/SKILL.md").is_file()

        page.goto(f"{base_url}/worlds/{world}/workflows/{workflow['workflow_id']}/scorecard")
        score = page.get_by_test_id("score")
        expect(score).to_have_count(1)
        expect(score).to_contain_text("done")
        expect(score).to_contain_text("1 of 1")
