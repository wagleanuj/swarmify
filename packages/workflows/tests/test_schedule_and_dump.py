"""Schedules are cron DBOS accepts; a run's stages dump back to YAML the parser reads."""

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from samples import WEEKLY_SHORT
from swarmify_workflows import Workflows, cron_problem, dump_workflow, next_fire
from swarmify_workflows.parse import parse_workflow


def test_cron_problem_accepts_five_and_six_fields_and_names_the_rest() -> None:
    assert cron_problem("0 8 * * 1") is None
    assert cron_problem("*/10 * * * * *") is None
    assert cron_problem("every monday") is not None
    assert cron_problem("0 8 * *") is not None


def test_next_fire_is_the_first_time_after_now_in_utc() -> None:
    monday_8 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert next_fire("0 8 * * 1", datetime(2026, 10, 26, 12, tzinfo=UTC)) == monday_8
    at = datetime(2026, 10, 26, 12, 0, 1, tzinfo=UTC)
    assert next_fire("*/5 * * * * *", at) == datetime(2026, 10, 26, 12, 0, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_fire("nope")


def test_a_bad_schedule_is_a_named_violation_and_the_model_still_parses() -> None:
    text = "name: x\nschedule: every monday\nstages:\n  - gate: g\n    ask: ok\n"
    model, violations = parse_workflow(text)
    assert model is not None and model.schedule == "every monday"
    assert [(v.rule, v.stage) for v in violations] == [("schedule", None)]


def test_dump_round_trips_a_parsed_model_and_drops_empty_fields() -> None:
    model, _ = parse_workflow(WEEKLY_SHORT)
    assert model is not None
    stages = []
    for stage in model.stages:
        fields = asdict(stage)
        stages.append((fields.pop("type"), fields.pop("name"), fields))
    text = dump_workflow(
        model.name,
        stages,
        schedule=model.schedule,
        budget_usd=model.budget_usd,
        agents=model.agents,
    )
    again, violations = parse_workflow(text)
    assert violations == [] and again == model
    assert "tools: []" not in text and "output_type: null" not in text
    assert text.startswith("name: Weekly short\nschedule: 0 8 * * 1\n")


def test_copy_saves_the_current_yaml_in_the_other_world(workflows: Workflows) -> None:
    seen = []
    workflows.on_saved = seen.append
    v1 = workflows.save("world_1", WEEKLY_SHORT)
    v2 = workflows.save("world_1", WEEKLY_SHORT.replace("Weekly", "Daily"), v1.workflow_id)
    copied = workflows.copy(v1.workflow_id, "world_2")
    assert copied.workflow_id != v1.workflow_id and copied.number == 1
    assert copied.yaml == v2.yaml and copied.model.name == "Daily short"
    assert [w.name for w in workflows.list_all("world_2")] == ["Daily short"]
    assert seen == [v1, v2, copied]
    with pytest.raises(KeyError):
        workflows.copy("wf_nope", "world_2")


def test_scheduled_lists_only_workflows_whose_current_version_has_a_cron(
    workflows: Workflows,
) -> None:
    timed = workflows.save("world_1", WEEKLY_SHORT)
    workflows.save("world_1", "name: Plain\nstages:\n  - gate: g\n    ask: ok\n")
    assert workflows.scheduled() == {timed.workflow_id: "0 8 * * 1"}
    plain = WEEKLY_SHORT.replace('schedule: "0 8 * * 1"\n', "")
    workflows.save("world_1", plain, timed.workflow_id)
    assert workflows.scheduled() == {}
