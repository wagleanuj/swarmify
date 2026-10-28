from samples import WEEKLY_SHORT
from swarmify_workflows import ConnectorStage, GateStage, SwarmStage, WorkflowStage
from swarmify_workflows.parse import parse_workflow


def rules(text: str) -> list[tuple[str, str | None, str]]:
    _, violations = parse_workflow(text)
    return [(v.rule, v.stage, v.message) for v in violations]


def test_the_weekly_short_parses_into_four_stage_types_with_their_fields() -> None:
    model, violations = parse_workflow(WEEKLY_SHORT)
    assert violations == [] and model is not None
    assert (model.name, model.schedule, model.budget_usd, model.agents) == (
        "Weekly short",
        "0 8 * * 1",
        5,
        4,
    )
    research, pick, script, render, review, publish = model.stages
    assert research == SwarmStage(
        "research",
        "Find 5 short-form video ideas trending this week.",
        "5 ideas ranked by the judge",
        tools=["web_search", "graph"],
        output_type="list[idea]",
    )
    assert pick == GateStage("pick", "Pick one idea. Optional note for the script.", "pick")
    assert isinstance(script, SwarmStage) and script.produces == ["script.md", "shots.md"]
    assert render == ConnectorStage("render", "remotion_render", "script.md", "short.mp4")
    assert review == GateStage("review", "Watch it. Approve or send back with a note.", "approve")
    assert publish == ConnectorStage(
        "youtube", "youtube", "short.mp4", "publish.md", {"visibility": "scheduled"}
    )


def test_a_workflow_stage_names_the_workflow_it_runs_and_what_it_takes() -> None:
    model, violations = parse_workflow(
        "name: Parent\nstages:\n  - workflow: trends\n    runs: wf_abc\n    take: [brief.md]\n"
    )
    assert violations == [] and model is not None
    assert model.stages == [WorkflowStage("trends", "wf_abc", ["brief.md"])]


def test_a_swarm_stage_may_name_the_model_its_agents_run_on() -> None:
    text = "name: N\nstages:\n  - swarm: s\n    goal: g\n    done_when: d\n    model: {m}\n"
    model, violations = parse_workflow(text.format(m="claude-haiku-4-5"))
    assert violations == [] and model is not None
    assert model.stages == [SwarmStage("s", "g", "d", model="claude-haiku-4-5")]
    assert rules(text.format(m="[1, 2]")) == [("schema", "s", "model must be a non-empty string")]


def test_broken_yaml_names_the_line() -> None:
    model, violations = parse_workflow("name: x\nstages:\n  - swarm: [\n")
    assert model is None
    [v] = violations
    assert v.rule == "yaml" and "line 4" in v.message


def test_non_mapping_documents_are_one_schema_violation() -> None:
    for text in ["", "- a\n- b", "just words", "42"]:
        model, violations = parse_workflow(text)
        assert model is None and [v.rule for v in violations] == ["schema"], text


def test_every_schema_problem_is_reported_not_just_the_first() -> None:
    found = rules(
        "name: ''\nbudget_usd: -1\nagents: 2.5\nextra: 1\nstages:\n"
        "  - swarm: research\n    tools: web_search\n"
        "  - gate: pick\n    kind: maybe\n"
        "  - gate: pick\n    ask: again\n"
        "  - connector: render\n    params: nope\n"
        "  - workflow: child\n"
        "  - 7\n"
        "  - swarm: a\n    gate: b\n"
        "  - swarm: ''\n"
    )
    assert ("schema", None, "name must be a non-empty string") in found
    assert ("schema", None, "budget_usd must be a positive number") in found
    assert ("schema", None, "agents must be a positive whole number") in found
    assert ("schema", None, "unknown field 'extra'") in found
    assert ("schema", "research", "goal is required") in found
    assert ("schema", "research", "done_when is required") in found
    assert ("schema", "research", "tools must be a list of names") in found
    assert ("schema", "pick", "kind must be one of pick, approve, confirm") in found
    assert ("schema", "pick", "stage name 'pick' is used twice") in found
    assert ("schema", "render", "params must be a mapping") in found
    assert ("schema", "child", "runs is required") in found
    assert ("schema", None, "stage 6 must be a mapping") in found
    assert (
        "schema",
        None,
        "stage 7 needs exactly one of swarm, gate, connector, workflow",
    ) in found
    assert ("schema", None, "stage 8: swarm must name the stage") in found


def test_missing_or_empty_stages_is_a_violation_but_the_name_still_parses() -> None:
    for text in ["name: x", "name: x\nstages: []", "name: x\nstages: nope"]:
        model, violations = parse_workflow(text)
        assert model is not None and model.stages == []
        assert ("schema", None, "stages must be a non-empty list") in [
            (v.rule, v.stage, v.message) for v in violations
        ], text


def test_unknown_stage_fields_are_named_per_stage() -> None:
    assert rules("name: x\nstages:\n  - gate: g\n    ask: a\n    goal: nope\n") == [
        ("schema", "g", "unknown field 'goal'")
    ]


def test_an_unquoted_timestamp_stays_the_text_written() -> None:
    model, violations = parse_workflow(
        "name: P\nstages:\n  - connector: youtube\n"
        "    params: {schedule: 2099-01-01T09:00:00Z, day: 2099-01-01}\n"
    )
    assert violations == [] and model is not None
    stage = model.stages[0]
    assert isinstance(stage, ConnectorStage)
    assert stage.params == {"schedule": "2099-01-01T09:00:00Z", "day": "2099-01-01"}
