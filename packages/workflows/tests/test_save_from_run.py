"""A Run Change saved to the workflow (Run Assistant spec, ADR 0027): the change applied to the
workflow's current version by stage name, the check's draft shown before anything is saved, and
a second confirmation when the workflow changed the touched stages since the run's version."""

from typing import Any

from samples import WEEKLY_SHORT
from swarmify_workflows import SwarmStage, Workflows

WORLD = "world_1"
SCRIPT = {
    "name": "script",
    "type": "swarm",
    "goal": "Write script and shot list for the pick, 20-25s.",
    "done_when": "script.md and shots.md written",
    "tools": ["graph"],
    "produces": ["script.md", "shots.md"],
    "agents": 4,
}


def stage(model: Any, name: str) -> Any:
    return next(s for s in model.stages if s.name == name)


def test_a_stage_edit_drafts_the_next_version_and_saves_only_when_asked(
    workflows: Workflows,
) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    edited = {**SCRIPT, "goal": "Write a 20s script with at most five hashtags."}

    draft = workflows.save_from_run(v1.workflow_id, 1, [SCRIPT], [edited])
    assert (draft.version, draft.run_version, draft.violations, draft.conflicts) == (1, 1, [], [])
    assert "at most five hashtags" in draft.yaml and "at most five" not in draft.before
    assert draft.saved is None and [v.number for v in workflows.versions(v1.workflow_id)] == [1]

    saved = workflows.save_from_run(v1.workflow_id, 1, [SCRIPT], [edited], save=True).saved
    assert saved is not None and saved.number == 2
    script = stage(saved.model, "script")
    assert isinstance(script, SwarmStage) and script.goal == edited["goal"]
    assert script.done_when == SCRIPT["done_when"] and script.agents == 4
    assert saved.model.schedule == "0 8 * * 1" and len(saved.model.stages) == 6
    assert "agents: 4" not in saved.yaml.split("stages:")[1], "unchanged fields keep their text"


def test_a_skip_removes_the_stage_and_a_restructure_replaces_the_stages_from_its_target_on(
    workflows: Workflows,
) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    pick = {"name": "pick", "type": "gate", "ask": "Pick one idea.", "kind": "pick"}
    skipped = workflows.save_from_run(v1.workflow_id, 1, [pick], [], save=True).saved
    assert skipped is not None
    assert [s.name for s in skipped.model.stages] == [
        "research", "script", "render", "review", "youtube",
    ]  # fmt: skip

    render = {"name": "render", "type": "connector", "calls": "remotion_render"}
    review = {"name": "review", "type": "gate", "ask": "Watch it."}
    youtube = {"name": "youtube", "type": "connector", "calls": "youtube"}
    check = {"name": "check", "type": "gate", "ask": "Read the script first.", "kind": "approve"}
    new = [check, {**render, "input": "script.md", "output": "short.mp4"}, review, youtube]
    restructured = workflows.save_from_run(
        v1.workflow_id, 2, [render, review, youtube], new, save=True
    ).saved
    assert restructured is not None
    assert [s.name for s in restructured.model.stages] == [
        "research", "script", "check", "render", "review", "youtube",
    ]  # fmt: skip
    assert stage(restructured.model, "youtube").params == {"visibility": "scheduled"}


def test_a_newer_version_that_changed_the_touched_stage_is_named_and_needs_confirming(
    workflows: Workflows,
) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    newer = WEEKLY_SHORT.replace("20-25s.", "15s.")
    workflows.save(WORLD, newer, v1.workflow_id)
    edited = {**SCRIPT, "done_when": "script.md written"}

    draft = workflows.save_from_run(v1.workflow_id, 1, [SCRIPT], [edited], save=True)
    assert draft.saved is None and draft.version == 2 and draft.run_version == 1
    assert draft.conflicts == ["Since this run's v1, v2 changed script (goal)."]

    saved = workflows.save_from_run(
        v1.workflow_id, 1, [SCRIPT], [edited], save=True, confirmed=True
    ).saved
    assert saved is not None and saved.number == 3
    script = stage(saved.model, "script")
    assert script.goal.endswith("15s.") and script.done_when == "script.md written"

    research = {"name": "research", "type": "swarm", "goal": "Find ideas.", "done_when": "found"}
    untouched = workflows.save_from_run(
        v1.workflow_id, 1, [research], [{**research, "done_when": "ranked"}]
    )
    assert untouched.conflicts == [] and untouched.violations == []


def test_a_change_the_check_refuses_or_a_stage_the_workflow_no_longer_has_never_saves(
    workflows: Workflows,
) -> None:
    v1 = workflows.save(WORLD, WEEKLY_SHORT)
    review = {"name": "review", "type": "gate", "ask": "Watch it."}
    refused = workflows.save_from_run(v1.workflow_id, 1, [review], [], save=True, confirmed=True)
    assert refused.saved is None and any(
        v.rule == "publish_without_gate" for v in refused.violations
    )

    gone = {"name": "outro", "type": "gate", "ask": "Fine?"}
    missing = workflows.save_from_run(v1.workflow_id, 1, [gone], [], save=True, confirmed=True)
    assert missing.saved is None
    assert [v.message for v in missing.violations] == ["v1 has no stage named outro"]
    assert [v.number for v in workflows.versions(v1.workflow_id)] == [1]
