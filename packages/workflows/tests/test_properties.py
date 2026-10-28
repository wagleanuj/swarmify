"""Property tests: generated workflows, the rules as invariants, and saves as a state machine."""

import tempfile
from dataclasses import replace
from pathlib import Path

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, invariant, rule

from samples import CONNECTORS, migrated_db
from swarmify_workflows import Invalid, SqliteStorage, Workflows
from swarmify_workflows.model import (
    ConnectorStage,
    GateStage,
    Stage,
    SwarmStage,
    WorkflowModel,
    WorkflowStage,
)
from swarmify_workflows.parse import parse_workflow
from swarmify_workflows.validate import validate

ARTIFACTS = ["ideas.md", "script.md", "short.mp4", "brief.md"]
KNOWN = ["wf_a", "wf_b", "wf_c"]
names = st.sampled_from(["a", "b", "c", "d", "e", "f"])
artifact = st.sampled_from(ARTIFACTS)
connector_name = st.sampled_from([*CONNECTORS, "missing"])

swarm = st.builds(
    SwarmStage,
    name=names,
    goal=st.just("goal"),
    done_when=st.just("done"),
    tools=st.lists(connector_name, max_size=3),
    produces=st.lists(artifact, max_size=2),
)
gate = st.builds(GateStage, name=names, ask=st.just("ask"))
connector = st.builds(
    ConnectorStage,
    name=names,
    calls=connector_name,
    input=st.none() | artifact,
    output=st.none() | artifact,
)
workflow_stage = st.builds(
    WorkflowStage, name=names, runs=st.sampled_from([*KNOWN, "wf_nope"]), take=st.lists(artifact)
)
stages: st.SearchStrategy[list[Stage]] = st.lists(
    st.one_of(swarm, gate, connector, workflow_stage), min_size=1, max_size=7
)
models = st.builds(WorkflowModel, name=st.just("w"), stages=stages)
references = st.fixed_dictionaries({k: st.lists(st.sampled_from(KNOWN), max_size=2) for k in KNOWN})


def clean_model(model: WorkflowModel) -> bool:
    return not validate(model, workflow_id=None, connectors=CONNECTORS, references={})


@given(models, references)
def test_a_passing_workflow_satisfies_every_rule_restated(model: WorkflowModel, refs: dict) -> None:
    found = validate(model, workflow_id="wf_a", connectors=CONNECTORS, references=refs)
    rules = {v.rule for v in found}
    produced: set[str] = set()
    for index, stage in enumerate(model.stages):
        if isinstance(stage, SwarmStage):
            for tool in stage.tools:
                ref = CONNECTORS.get(tool)
                assert (ref is None) <= ("unknown_connector" in rules)
                assert (ref is not None and "swarm" not in ref.stage_types) <= (
                    "connector_stage_type" in rules
                )
            assert ("short.mp4" not in stage.produces) or ("swarm_binary" in rules)
            produced |= set(stage.produces)
        elif isinstance(stage, ConnectorStage):
            ref = CONNECTORS.get(stage.calls)
            if ref is not None and ref.kind == "publish":
                gated = index > 0 and isinstance(model.stages[index - 1], GateStage)
                assert gated or "publish_without_gate" in rules
            if stage.input is not None and stage.input not in produced:
                assert "input_not_produced" in rules
            if stage.output:
                produced.add(stage.output)
        elif isinstance(stage, WorkflowStage):
            assert stage.runs in KNOWN or "unknown_workflow" in rules
            produced |= set(stage.take)
    if not rules:
        # no violations means every stage passed the checks above with room to spare
        assert all(c.calls in CONNECTORS for c in model.stages if isinstance(c, ConnectorStage))


@given(models)
def test_validation_is_deterministic_and_every_violation_names_a_known_rule(
    model: WorkflowModel,
) -> None:
    once = validate(model, workflow_id=None, connectors=CONNECTORS, references={})
    twice = validate(model, workflow_id=None, connectors=CONNECTORS, references={})
    assert once == twice
    assert {v.rule for v in once} <= {
        "unknown_connector",
        "connector_stage_type",
        "publish_without_gate",
        "input_not_produced",
        "connector_params",
        "swarm_binary",
        "unknown_workflow",
        "cycle",
        "depth",
    }
    for v in once:
        if v.rule not in ("cycle", "depth"):
            assert v.stage in {s.name for s in model.stages}


@given(models.filter(clean_model))
def test_inserting_a_publish_stage_anywhere_but_after_a_gate_is_caught(
    model: WorkflowModel,
) -> None:
    publish = ConnectorStage("publish_it", "youtube")
    for index in range(len(model.stages) + 1):
        mutated = WorkflowModel("w", [*model.stages[:index], publish, *model.stages[index:]])
        found = validate(mutated, workflow_id=None, connectors=CONNECTORS, references={})
        gated = index > 0 and isinstance(model.stages[index - 1], GateStage)
        on_it = {v.rule for v in found if v.stage == "publish_it"}
        assert ("publish_without_gate" in on_it) == (not gated)


@given(models.filter(clean_model))
def test_a_model_survives_a_yaml_round_trip(model: WorkflowModel) -> None:
    unique = [replace(s, name=f"{s.name}{n}") for n, s in enumerate(model.stages)]
    model = WorkflowModel(model.name, unique)
    doc = {"name": model.name, "stages": [_to_yaml(s) for s in model.stages]}
    parsed, violations = parse_workflow(yaml.safe_dump(doc))
    assert violations == [] and parsed is not None
    assert parsed.stages == model.stages


def _to_yaml(stage: Stage) -> dict:
    match stage:
        case SwarmStage():
            return {
                "swarm": stage.name,
                "goal": stage.goal,
                "done_when": stage.done_when,
                "tools": stage.tools,
                "produces": stage.produces,
            }
        case GateStage():
            return {"gate": stage.name, "ask": stage.ask, "kind": stage.kind}
        case ConnectorStage():
            doc: dict = {"connector": stage.name, "calls": stage.calls}
            if stage.input:
                doc["input"] = stage.input
            if stage.output:
                doc["output"] = stage.output
            return doc
        case WorkflowStage():
            return {"workflow": stage.name, "runs": stage.runs, "take": stage.take}


@given(st.text(max_size=200))
def test_any_text_parses_to_a_report_never_an_exception(text: str) -> None:
    model, violations = parse_workflow(text)
    assert model is not None or violations


class SavesKeepTheStoreSound(RuleBasedStateMachine):
    """Any sequence of saves leaves every stored workflow acyclic and at most two deep."""

    workflows_ = Bundle("workflows")

    @initialize()
    def open_store(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Workflows(
            SqliteStorage(migrated_db(Path(self.tmp.name))), lambda _: CONNECTORS
        )
        self.ids: list[str] = []

    @rule(target=workflows_, children=st.lists(st.integers(min_value=0, max_value=5), max_size=2))
    def create(self, children: list[int]) -> str:
        return self._save(None, children)

    @rule(workflow=workflows_, children=st.lists(st.integers(min_value=0, max_value=5), max_size=2))
    def new_version(self, workflow: str, children: list[int]) -> None:
        self._save(workflow, children)

    def _save(self, workflow_id: str | None, children: list[int]) -> str:
        picks = [self.ids[i % len(self.ids)] for i in children if self.ids]
        text = "name: w\nstages:\n  - gate: g\n    ask: ok\n" + "".join(
            f"  - workflow: s{n}\n    runs: {child}\n" for n, child in enumerate(picks)
        )
        try:
            version = self.service.save("world_1", text, workflow_id)
        except Invalid as exc:
            assert {v.rule for v in exc.violations} <= {"cycle", "depth"}
            return workflow_id or self.ids[0]
        if workflow_id is None:
            self.ids.append(version.workflow_id)
        return version.workflow_id

    @invariant()
    def store_is_acyclic_and_shallow(self) -> None:
        graph = {}
        for workflow_id, text in self.service.storage.current_yaml().items():
            model, _ = parse_workflow(text)
            stages = model.stages if model else []
            graph[workflow_id] = [s.runs for s in stages if isinstance(s, WorkflowStage)]
        for node in graph:
            assert not _reaches(graph, node, node), f"cycle through {node}"
            assert _depth(graph, node) <= 2, f"{node} nests deeper than two"

    def teardown(self) -> None:
        self.tmp.cleanup()


def _reaches(graph: dict[str, list[str]], start: str, target: str) -> bool:
    seen, todo = set(), list(graph[start])
    while todo:
        node = todo.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(graph.get(node, []))
    return False


def _depth(graph: dict[str, list[str]], node: str) -> int:
    return max((1 + _depth(graph, k) for k in graph.get(node, [])), default=0)


TestSavesKeepTheStoreSound = SavesKeepTheStoreSound.TestCase
TestSavesKeepTheStoreSound.settings = settings(
    max_examples=25, stateful_step_count=15, deadline=None
)
