import pytest

from samples import CONNECTORS
from swarmify_workflows.model import (
    ConnectorRef,
    ConnectorStage,
    GateStage,
    SwarmStage,
    WorkflowModel,
    WorkflowStage,
)
from swarmify_workflows.validate import validate

GATE = GateStage("review", "Look.")


def rules(model: WorkflowModel, workflow_id: str | None = None, **references: list[str]) -> list:
    found = validate(model, workflow_id=workflow_id, connectors=CONNECTORS, references=references)
    return sorted((v.rule, v.stage) for v in found)


def test_publish_must_come_right_after_a_gate() -> None:
    publish = ConnectorStage("publish", "youtube")
    render = ConnectorStage("render", "remotion_render", output="short.mp4")
    assert rules(WorkflowModel("w", [GATE, publish])) == []
    assert rules(WorkflowModel("w", [publish])) == [("publish_without_gate", "publish")]
    assert rules(WorkflowModel("w", [GATE, render, publish])) == [
        ("publish_without_gate", "publish")
    ]
    assert rules(WorkflowModel("w", [render, GATE, publish])) == []


def test_a_private_post_needs_no_gate() -> None:
    private = ConnectorStage("upload", "youtube", params={"visibility": "private"})
    public = ConnectorStage("upload", "youtube", params={"visibility": "public"})
    unsaid = ConnectorStage("upload", "youtube")  # the connector's default is not read
    assert rules(WorkflowModel("w", [private])) == []
    assert rules(WorkflowModel("w", [public])) == [("publish_without_gate", "upload")]
    assert rules(WorkflowModel("w", [unsaid])) == [("publish_without_gate", "upload")]


def test_unknown_connectors_and_wrong_stage_types_are_named() -> None:
    swarm = SwarmStage("research", "g", "d", tools=["web_search", "remotion_render", "nope"])
    assert rules(WorkflowModel("w", [swarm])) == [
        ("connector_stage_type", "research"),
        ("unknown_connector", "research"),
    ]
    assert rules(WorkflowModel("w", [ConnectorStage("x", "missing")])) == [
        ("unknown_connector", "x")
    ]


def test_agent_tools_need_no_connector_and_belong_to_swarm_stages() -> None:
    swarm = SwarmStage("research", "g", "d", tools=["web_search", "graph"])
    assert rules(WorkflowModel("w", [swarm])) == []
    found = validate(
        WorkflowModel("w", [ConnectorStage("search", "web_search")]),
        workflow_id=None,
        connectors=CONNECTORS,
        references={},
    )
    assert [(v.rule, v.message) for v in found] == [
        ("connector_stage_type", "'web_search' is an Agent Tool: list it in a swarm stage's tools")
    ]
    unknown = SwarmStage("research", "g", "d", tools=["nope"])
    found = validate(
        WorkflowModel("w", [unknown]), workflow_id=None, connectors=CONNECTORS, references={}
    )
    assert [v.message for v in found] == ["no connector or Agent Tool named 'nope'"]


def test_connector_inputs_must_be_produced_by_an_earlier_stage() -> None:
    script = SwarmStage("script", "g", "d", produces=["script.md"])
    render = ConnectorStage("render", "remotion_render", input="script.md", output="short.mp4")
    upload = ConnectorStage("upload", "remotion_render", input="short.mp4")
    assert rules(WorkflowModel("w", [script, render, upload])) == []
    assert rules(WorkflowModel("w", [render, script])) == [("input_not_produced", "render")]
    assert rules(WorkflowModel("w", [upload, render, script])) == [
        ("input_not_produced", "render"),
        ("input_not_produced", "upload"),
    ]
    child = WorkflowStage("child", "wf_c", take=["short.mp4"])
    assert rules(WorkflowModel("w", [child, upload]), wf_c=[]) == []


def test_every_one_of_several_inputs_must_be_produced_by_an_earlier_stage() -> None:
    script = SwarmStage("script", "g", "d", produces=["script.md"])
    voice = ConnectorStage("voice", "remotion_render", input="script.md", output="timings.json")
    video = ConnectorStage("video", "remotion_render", inputs=["script.md", "timings.json"])
    assert rules(WorkflowModel("w", [script, voice, video])) == []
    found = validate(
        WorkflowModel("w", [script, video, voice]),
        workflow_id=None,
        connectors=CONNECTORS,
        references={},
    )
    assert [(v.rule, v.stage, v.message) for v in found] == [
        ("input_not_produced", "video", "input 'timings.json' is not produced by an earlier stage")
    ]


def test_workflow_stages_must_name_a_stored_workflow() -> None:
    assert rules(WorkflowModel("w", [WorkflowStage("s", "wf_x")])) == [("unknown_workflow", "s")]
    assert rules(WorkflowModel("w", [WorkflowStage("s", "wf_x")]), wf_x=[]) == []


def test_cycles_across_workflow_stages_are_refused() -> None:
    me = WorkflowModel("w", [WorkflowStage("s", "wf_b")])
    assert rules(me, "wf_a", wf_a=[], wf_b=[]) == []
    assert rules(me, "wf_a", wf_a=[], wf_b=["wf_a"]) == [("cycle", None)]
    assert rules(me, "wf_a", wf_a=[], wf_b=["wf_c"], wf_c=["wf_a"]) == [("cycle", None)]
    assert rules(WorkflowModel("w", [WorkflowStage("s", "wf_a")]), "wf_a", wf_a=[]) == [
        ("cycle", None)
    ]


@pytest.mark.parametrize(
    ("references", "expected"),
    [
        ({"wf_b": []}, []),
        ({"wf_b": ["wf_c"], "wf_c": []}, []),
        ({"wf_b": ["wf_c"], "wf_c": ["wf_d"], "wf_d": []}, [("depth", None)]),
        # a parent already runs this workflow, so the chain is counted upward too
        ({"wf_b": ["wf_c"], "wf_c": [], "wf_p": ["wf_a"]}, [("depth", None)]),
        ({"wf_b": [], "wf_p": ["wf_a"], "wf_g": ["wf_p"]}, [("depth", None)]),
        ({"wf_b": [], "wf_p": ["wf_a"]}, []),
    ],
)
def test_nesting_deeper_than_two_is_refused(references: dict, expected: list) -> None:
    me = WorkflowModel("w", [WorkflowStage("s", "wf_b")])
    assert rules(me, "wf_a", wf_a=[], **references) == expected


def test_a_new_workflow_with_no_workflow_stages_has_nothing_to_check() -> None:
    assert rules(WorkflowModel("w", [GATE]), None, wf_x=["wf_y"], wf_y=[]) == []


def test_a_connector_s_own_param_check_is_reported_per_problem() -> None:
    seen: list[dict] = []

    def problems(config: dict) -> list[str]:
        seen.append(config)
        return [] if config["params"].get("title") else ["a publish stage needs params.title"]

    youtube = ConnectorRef("youtube", "publish", frozenset({"connector"}), problems)
    connectors = {**CONNECTORS, "youtube": youtube}
    bare = ConnectorStage("publish", "youtube", input="short.mp4")
    child = WorkflowStage("child", "wf_c", take=["short.mp4"])
    found = validate(
        WorkflowModel("w", [child, GATE, bare]),
        workflow_id=None,
        connectors=connectors,
        references={"wf_c": []},
    )
    assert [(v.rule, v.stage, v.message) for v in found] == [
        ("connector_params", "publish", "a publish stage needs params.title")
    ]
    assert seen == [
        {"calls": "youtube", "input": "short.mp4", "inputs": [], "output": None, "params": {}}
    ]


@pytest.mark.parametrize("name", ["video.mp4", "cover.png", "voice.mp3", "deck.pdf", "a.zip"])
def test_a_swarm_stage_cannot_promise_a_binary_file(name: str) -> None:
    found = validate(
        WorkflowModel("w", [SwarmStage("build", "g", "d", produces=["script.md", name])]),
        workflow_id=None,
        connectors=CONNECTORS,
        references={},
    )
    assert [(v.rule, v.stage) for v in found] == [("swarm_binary", "build")]
    assert f"only a connector stage can make {name}" in found[0].message
    assert "Build Session" in found[0].message


@pytest.mark.parametrize(
    "name",
    [
        "script.md",
        "ideas.json",
        "notes.txt",
        "plan.yaml",
        "x.svg",
        "captions.srt",
        "comp.ts",
        "run.sh",
        "notes.rtf",
    ],
)
def test_a_swarm_stage_may_produce_text(name: str) -> None:
    assert rules(WorkflowModel("w", [SwarmStage("s", "g", "d", produces=[name])])) == []


# Typed contracts (issue 133), on a fixture connector declaring a non-video type.
RECIPE = {"type": "object", "properties": {"steps": {"type": "array"}}, "required": ["steps"]}
COOK = ConnectorRef(
    "cook",
    "stage",
    frozenset({"connector"}),
    types={"recipe": RECIPE},
    input_types=("recipe", None),
)
TYPED = {**CONNECTORS, "cook": COOK}


def typed_rules(*stages: object, connectors: dict = TYPED) -> list[tuple[str, str | None, str]]:
    model = WorkflowModel("w", list(stages))  # type: ignore[arg-type]
    found = validate(model, workflow_id=None, connectors=connectors, references={})
    return [(v.rule, v.stage, v.message) for v in found]


def test_a_typed_input_needs_its_swarm_stage_to_promise_the_type() -> None:
    cook = ConnectorStage("cook it", "cook", inputs=["recipe.md", "notes.md"])
    notes = SwarmStage("notes", "g", "d", produces=["notes.md"])
    promised = SwarmStage("write", "g", "d", output_type="recipe", produces=["recipe.md"])
    assert typed_rules(promised, notes, cook) == []

    missing = SwarmStage("write", "g", "d", produces=["recipe.md"])
    assert typed_rules(missing, notes, cook) == [
        (
            "output_type",
            "cook it",
            "stage 'cook it' reads recipe.md from swarm stage 'write' as type recipe, but"
            " 'write' declares none; give it output_type: recipe",
        )
    ]
    other = SwarmStage("write", "g", "d", output_type="list[recipe]", produces=["recipe.md"])
    ((rule, _, message),) = typed_rules(other, notes, cook)
    assert rule == "output_type" and "declares output_type list[recipe]" in message
    # untyped inputs, and a file a connector stage made, carry no promise to check
    made = ConnectorStage("fetch", "cook", output="recipe.md")
    assert typed_rules(promised, notes, made, cook) == []


def test_two_connectors_declaring_one_type_differently_is_a_check_error() -> None:
    same = ConnectorRef("bake", "stage", frozenset({"connector"}), types={"recipe": RECIPE})
    differs = ConnectorRef("bake", "stage", frozenset({"connector"}), types={"recipe": {}})
    write = SwarmStage("write", "g", "d", output_type="recipe", produces=["recipe.md"])
    assert typed_rules(write, connectors={**TYPED, "bake": same}) == []
    assert typed_rules(write, connectors={**TYPED, "bake": differs}) == [
        (
            "type_conflict",
            "write",
            "connectors cook and bake declare type recipe with different schemas",
        )
    ]
