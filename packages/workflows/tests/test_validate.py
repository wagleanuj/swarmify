import pytest

from samples import CONNECTORS
from swarmify_workflows.model import (
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


def test_unknown_connectors_and_wrong_stage_types_are_named() -> None:
    swarm = SwarmStage("research", "g", "d", tools=["web_search", "remotion_render", "nope"])
    assert rules(WorkflowModel("w", [swarm])) == [
        ("connector_stage_type", "research"),
        ("unknown_connector", "research"),
    ]
    assert rules(WorkflowModel("w", [ConnectorStage("search", "web_search")])) == [
        ("connector_stage_type", "search")
    ]
    assert rules(WorkflowModel("w", [ConnectorStage("x", "missing")])) == [
        ("unknown_connector", "x")
    ]


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
