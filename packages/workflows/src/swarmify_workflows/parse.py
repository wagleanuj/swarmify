"""Workflow YAML to a WorkflowModel. Every schema problem is a named Violation; nothing raises."""

from typing import Any

import yaml

from swarmify_workflows.model import (
    GATE_KINDS,
    STAGE_TYPES,
    ConnectorStage,
    GateStage,
    Stage,
    SwarmStage,
    Violation,
    WorkflowModel,
    WorkflowStage,
)
from swarmify_workflows.schedule import CRON_RULE, cron_problem

TOP_FIELDS = {"name", "schedule", "budget_usd", "agents", "stages"}
STAGE_FIELDS: dict[str, set[str]] = {
    "swarm": {"goal", "done_when", "tools", "graphs", "output_type", "produces"},
    "gate": {"ask", "kind"},
    "connector": {"calls", "input", "output", "params"},
    "workflow": {"runs", "take"},
}


class _Loader(yaml.SafeLoader):
    """Safe YAML that leaves timestamps as the text written, so `schedule: 2099-01-01T09:00:00Z`
    stays a string a stage config can carry through JSON."""


_Loader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: node.value)


class _Fields:
    """Typed reads from one mapping, recording a violation per bad field instead of raising."""

    def __init__(self, data: dict[str, Any], out: list[Violation], stage: str | None) -> None:
        self.data, self.out, self.stage = data, out, stage

    def fail(self, message: str) -> None:
        self.out.append(Violation("schema", message, self.stage))

    def text(self, key: str, *, required: bool = False) -> str | None:
        value = self.data.get(key)
        if value is None:
            if required:
                self.fail(f"{key} is required")
            return None
        if not isinstance(value, str) or not value.strip():
            self.fail(f"{key} must be a non-empty string")
            return None
        return value.strip()

    def names(self, key: str) -> list[str]:
        value = self.data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            self.fail(f"{key} must be a list of names")
            return []
        return value

    def number(self, key: str, *, integer: bool = False) -> float | int | None:
        value = self.data.get(key)
        if value is None:
            return None
        ok = isinstance(value, int) if integer else isinstance(value, int | float)
        if not ok or isinstance(value, bool) or value <= 0:
            self.fail(f"{key} must be a positive {'whole ' if integer else ''}number")
            return None
        return value

    def unknown(self, allowed: set[str]) -> None:
        for key in sorted(set(self.data) - allowed):
            self.fail(f"unknown field '{key}'")


def parse_workflow(text: str) -> tuple[WorkflowModel | None, list[Violation]]:
    """Return (model, violations). The model is None only when the document has no usable shape."""
    try:
        doc = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "could not parse"
        return None, [Violation("yaml", f"not valid YAML{where}: {problem}")]
    if not isinstance(doc, dict):
        return None, [Violation("schema", "a workflow is a mapping with a name and stages")]

    out: list[Violation] = []
    top = _Fields(doc, out, None)
    name = top.text("name", required=True)
    schedule = top.text("schedule")
    if schedule is not None and (problem := cron_problem(schedule)) is not None:
        out.append(Violation(CRON_RULE, problem))
    budget = top.number("budget_usd")
    agents = top.number("agents", integer=True)
    top.unknown(TOP_FIELDS)
    raw_stages = doc.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        top.fail("stages must be a non-empty list")
        raw_stages = []

    stages: list[Stage] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_stages, start=1):
        stage_name, stage = _parse_stage(index, raw, out)
        if stage_name in seen:
            out.append(Violation("schema", f"stage name '{stage_name}' is used twice", stage_name))
        if stage_name is not None:
            seen.add(stage_name)
        if stage is not None:
            stages.append(stage)
    if name is None:
        return None, out
    model = WorkflowModel(
        name, stages, schedule, budget, int(agents) if agents is not None else None
    )
    return model, out


def _parse_stage(index: int, raw: Any, out: list[Violation]) -> tuple[str | None, Stage | None]:
    """Return (stage name, stage). The name is known even when the stage's own fields fail."""
    label = f"stage {index}"
    if not isinstance(raw, dict):
        out.append(Violation("schema", f"{label} must be a mapping", None))
        return None, None
    kinds = [t for t in STAGE_TYPES if t in raw]
    if len(kinds) != 1:
        out.append(
            Violation("schema", f"{label} needs exactly one of {', '.join(STAGE_TYPES)}", None)
        )
        return None, None
    kind = kinds[0]
    stage_name = raw[kind]
    if not isinstance(stage_name, str) or not stage_name.strip():
        out.append(Violation("schema", f"{label}: {kind} must name the stage", None))
        return None, None
    stage_name = stage_name.strip()
    f = _Fields(raw, out, stage_name)
    f.unknown(STAGE_FIELDS[kind] | {kind})
    before = len(out)
    stage: Stage | None = None
    if kind == "swarm":
        goal, done_when = f.text("goal", required=True), f.text("done_when", required=True)
        tools, graphs, produces = f.names("tools"), f.names("graphs"), f.names("produces")
        output_type = f.text("output_type")
        if goal and done_when:
            stage = SwarmStage(stage_name, goal, done_when, tools, graphs, output_type, produces)
    elif kind == "gate":
        ask = f.text("ask", required=True)
        gate_kind = f.text("kind") or "approve"
        if gate_kind not in GATE_KINDS:
            f.fail(f"kind must be one of {', '.join(GATE_KINDS)}")
        elif ask:
            stage = GateStage(stage_name, ask, gate_kind)
    elif kind == "connector":
        calls = f.text("calls") or stage_name
        input_, output = f.text("input"), f.text("output")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            f.fail("params must be a mapping")
        else:
            stage = ConnectorStage(stage_name, calls, input_, output, params)
    else:
        runs = f.text("runs", required=True)
        take = f.names("take")
        if runs:
            stage = WorkflowStage(stage_name, runs, take)
    return stage_name, (stage if len(out) == before else None)
