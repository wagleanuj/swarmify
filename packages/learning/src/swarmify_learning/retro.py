"""The Retro, as pure functions: the evidence of a run folded from the world's event stream,
the manual interventions that recur across runs of a workflow, the prompt a retro session gets,
and the parse of its answer into zero or one Skill Proposal plus zero or one Workflow Suggestion.

Learning reads events only (Context Map): `run.updated` carries the run with its stages and
gate decisions, `verdict.created` the judge's verdicts, `task.updated` the tasks people added,
`agent.logged` each line of a stage's live log.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from swarmify_kernel import Event
from swarmify_learning.model import ACTIONS, SKILL_NAME, SkillChange, WorkflowSuggestion

RECURRENCE = 3
"""The same manual intervention in this many runs makes a Workflow Suggestion worth raising."""
OPERATOR = "operator"
LOG_TAIL = 30
"""How many of a failed stage's last log lines the retro reads."""
RETRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {
            "type": ["object", "null"],
            "description": "At most one skill change, or null when the run shows no need",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "name": {"type": "string", "description": "Lowercase, digits and hyphens"},
                "content": {
                    "type": "string",
                    "description": "The whole SKILL.md: frontmatter with name and description,"
                    " then the procedure. Empty when retiring.",
                },
                "summary": {"type": "string", "description": "One sentence: what changes"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Verdicts, send-backs or notes that show the need",
                },
            },
            "required": ["action", "name", "summary", "evidence"],
        },
        "workflow": {
            "type": ["object", "null"],
            "description": "The workflow YAML changed to absorb a recurring intervention, or null",
            "properties": {
                "summary": {"type": "string"},
                "yaml": {"type": "string", "description": "The whole workflow YAML, changed"},
                "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["summary", "yaml", "evidence"],
        },
    },
}
SYSTEM = (
    "You are the retro of a swarm. After a run you read what the judge decided, what people"
    " sent back or noted at gates, and what earlier retros proposed, then propose at most one"
    " skill change, with evidence, or nothing. A skill is a procedure agents load when it"
    " matches a stage; it is written as SKILL.md with frontmatter (name, description) and a"
    " short body. Propose a change only when the evidence shows the same miss would recur"
    " without it. Answer only with the JSON the schema asks for."
)


@dataclass(frozen=True)
class Intervention:
    """A manual act on a run: a send-back, a gate note, or a task a person added."""

    run_id: str
    kind: str
    """send_back, note or task."""
    stage: str
    what: str
    """The target stage of a send-back, the note, or the task title."""


@dataclass(frozen=True)
class Evidence:
    """What one run shows the retro."""

    run_id: str
    name: str
    workflow_id: str | None
    status: str
    send_backs: int
    verdicts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)
    failure: list[str] = field(default_factory=list)
    """The failed stage and its error, then the tail of its log. Empty when nothing failed."""


@dataclass(frozen=True)
class Recurrence:
    """The same intervention in RECURRENCE or more runs of one workflow."""

    kind: str
    stage: str
    what: str
    runs: list[str]


@dataclass(frozen=True)
class Retro:
    skill: SkillChange | None
    workflow: WorkflowSuggestion | None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!")


def _runs(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """The newest `run.updated` payload per run."""
    runs: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type == "run.updated":
            runs[e.payload["id"]] = e.payload
    return runs


def _interventions(run: dict[str, Any], tasks: list[dict[str, Any]]) -> list[Intervention]:
    out = []
    for d in run.get("decisions") or []:
        if d.get("send_back_to"):
            out.append(Intervention(run["id"], "send_back", d["stage_name"], d["send_back_to"]))
        elif (d.get("note") or "").strip():
            out.append(Intervention(run["id"], "note", d["stage_name"], d["note"].strip()))
    names = {s["id"]: s["name"] for s in run.get("stages") or []}
    for t in tasks:
        out.append(Intervention(run["id"], "task", names.get(t["stage_id"], ""), t["title"]))
    return out


def _operator_tasks(events: Iterable[Event]) -> dict[str, list[dict[str, Any]]]:
    """The tasks people added, by run, each once."""
    seen: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type == "task.updated" and e.payload.get("added_by") == OPERATOR:
            seen[e.payload["id"]] = e.payload
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in seen.values():
        by_run[t["run_id"]].append(t)
    return by_run


def evidence(events: list[Event], run_id: str) -> Evidence:
    """What the run shows: its verdicts, gate decisions and interventions, as lines."""
    run = _runs(events).get(run_id) or {"id": run_id, "name": run_id, "status": "", "stages": []}
    names = {s["id"]: s["name"] for s in run.get("stages") or []}
    verdicts = []
    for e in events:
        p = e.payload
        if e.type == "verdict.created" and p.get("run_id") == run_id:
            outcome = "passed" if p.get("passed") else "failed"
            line = f"{names.get(p['stage_id'], p['stage_id'])} loop {p.get('loop')}: {outcome}"
            line += f", because {'; '.join(p.get('reasons') or [])}"
            if p.get("gaps"):
                line += f" (gaps: {'; '.join(p['gaps'])})"
            verdicts.append(line)
    decisions = []
    for d in run.get("decisions") or []:
        if d.get("send_back_to"):
            what = f"sent back to {d['send_back_to']}"
        elif d.get("kind") == "pick":
            what = f"picked {d.get('choice')}"
        else:
            what = d.get("choice") or ("confirmed" if d.get("kind") == "confirm" else "approved")
        note = f" with the note: {d['note'].strip()}" if (d.get("note") or "").strip() else ""
        decisions.append(f"at {d['stage_name']}: {what}{note}")
    tasks = _operator_tasks(events).get(run_id, [])
    failure: list[str] = []
    status = str(run.get("status") or "")
    # The retro of a failed run runs before the run ends, so a failed stage says how it ends.
    if failed := next((s for s in run.get("stages") or [] if s.get("status") == "failed"), None):
        status = "failed"
        failure.append(f"{failed['name']} failed: {failed.get('error') or 'no error recorded'}")
        log = [
            e.payload
            for e in events
            if e.type == "agent.logged" and e.payload.get("stage_id") == failed["id"]
        ]
        for p in log[-LOG_TAIL:]:
            tool = f" {p['tool']}" if p.get("tool") else ""
            detail = f": {p['detail']}" if p.get("detail") else ""
            failure.append(f"{p['agent']} {p['kind']}{tool}{detail}")
    return Evidence(
        run_id,
        str(run.get("name") or run_id),
        run.get("workflow_id"),
        status,
        int(run.get("send_backs") or 0),
        verdicts,
        decisions,
        _interventions(run, tasks),
        failure,
    )


def recurrences(events: list[Event], workflow_id: str) -> list[Recurrence]:
    """The interventions that recur in RECURRENCE or more runs of the workflow: the same
    send-back (from a gate to a stage), the same note at a gate, or a task with the same title."""
    tasks = _operator_tasks(events)
    runs_of: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for run in _runs(events).values():
        if run.get("workflow_id") != workflow_id:
            continue
        for i in _interventions(run, tasks.get(run["id"], [])):
            runs_of[(i.kind, i.stage, _norm(i.what))].add(run["id"])
    return [
        Recurrence(kind, stage, what, sorted(runs))
        for (kind, stage, what), runs in sorted(runs_of.items())
        if len(runs) >= RECURRENCE
    ]


def prompt(
    run: Evidence,
    recurring: list[Recurrence],
    skills: list[tuple[str, str]],
    priors: list[str],
    workflow_yaml: str | None,
) -> str:
    """What the retro session reads. `skills` are (name, SKILL.md) the world's agents load,
    `priors` one line per earlier proposal of the world."""
    lines = [
        f"Retro of run {run.name} ({run.run_id}), which ended {run.status or 'unknown'}"
        f" with {run.send_backs} send-back(s).",
        "# Verdicts",
        *(f"- {v}" for v in run.verdicts or ["(no swarm stage was judged)"]),
        "# Gate decisions",
        *(f"- {d}" for d in run.decisions or ["(no gate was answered)"]),
        "# Tasks people added by hand",
        *(f"- {i.stage}: {i.what}" for i in run.interventions if i.kind == "task"),
    ]
    if run.failure:
        lines += ["# The failure: the failed stage, then the last lines of its log", *run.failure]
    lines.append("# Skills the agents load now")
    for name, content in skills or []:
        lines += [f"## {name}", content.strip()]
    if not skills:
        lines.append("(none yet)")
    lines += ["# Earlier retros", *(f"- {p}" for p in priors or ["(none yet)"])]
    if recurring:
        lines.append(
            f"# Interventions repeated in {RECURRENCE} or more runs of this workflow"
            " (propose the workflow change that absorbs each)"
        )
        for r in recurring:
            lines.append(f"- {r.kind} at {r.stage}: {r.what} (runs {', '.join(r.runs)})")
        lines += ["# The workflow's YAML, as pinned by this run", workflow_yaml or "(ad hoc run)"]
    lines.append(
        "Answer with `skill` null, or one change (new, changed or retired) with its whole"
        " SKILL.md and the evidence lines above that call for it"
        + (", and `workflow` with the whole YAML changed." if recurring else ".")
    )
    return "\n".join(lines)


def frontmatter(content: str) -> dict[str, str]:
    """The `key: value` lines between the leading `---` fences of a SKILL.md."""
    text = content.lstrip()
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        return {}
    head = text[4:].split("\n---", 1)[0]
    out: dict[str, str] = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip().strip("\"'")
    return out


def check_skill(name: str, content: str) -> None:
    """Raise ValueError unless `content` is a SKILL.md for `name` the Agent SDK will load."""
    if not SKILL_NAME.match(name):
        raise ValueError(f"{name!r} is not a skill name: lowercase letters, digits and hyphens")
    front = frontmatter(content)
    if front.get("name") != name:
        raise ValueError(f"the SKILL.md frontmatter must say name: {name}")
    if not front.get("description"):
        raise ValueError("the SKILL.md frontmatter needs a description")


def _strings(value: Any) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def parse_retro(output: Any) -> Retro:
    """Lenient about what is absent (no proposal), strict about what is present."""
    if output is None:
        return Retro(None, None)
    if not isinstance(output, dict):
        raise ValueError(f"expected a JSON object, got {type(output).__name__}")
    skill = workflow = None
    if (raw := output.get("skill")) is not None:
        if not isinstance(raw, dict):
            raise ValueError("skill must be an object or null")
        action, name = str(raw.get("action") or ""), str(raw.get("name") or "").strip()
        content, evidence = str(raw.get("content") or ""), _strings(raw.get("evidence"))
        if action not in ACTIONS:
            raise ValueError(f"skill action must be one of {', '.join(ACTIONS)}")
        if not evidence:
            raise ValueError("a skill proposal needs evidence")
        if action == "retired":
            if not SKILL_NAME.match(name):
                raise ValueError(f"{name!r} is not a skill name")
            content = ""
        else:
            check_skill(name, content)
        summary = str(raw.get("summary") or "").strip() or f"{action} skill {name}"
        skill = SkillChange(action, name, content, summary, evidence)
    if (raw := output.get("workflow")) is not None:
        if not isinstance(raw, dict):
            raise ValueError("workflow must be an object or null")
        yaml = str(raw.get("yaml") or "")
        if not yaml.strip():
            raise ValueError("a workflow suggestion needs the YAML")
        evidence = _strings(raw.get("evidence"))
        if not evidence:
            raise ValueError("a workflow suggestion needs evidence")
        summary = str(raw.get("summary") or "").strip() or "Change the workflow"
        workflow = WorkflowSuggestion(summary, yaml, evidence)
    return Retro(skill, workflow)


def proposal_markdown(skill: SkillChange | None, workflow: WorkflowSuggestion | None) -> str:
    """The proposal artifact's body; the frontmatter is the caller's."""
    lines = ["# Retro proposal", ""]
    if skill:
        lines += [f"## Skill {skill.name}: {skill.action}", "", skill.summary, "", "Evidence:", ""]
        lines += [f"- {e}" for e in skill.evidence]
        if skill.content:
            lines += ["", "```markdown", skill.content.strip(), "```"]
    if workflow:
        lines += ["", "## Workflow suggestion", "", workflow.summary, "", "Evidence:", ""]
        lines += [f"- {e}" for e in workflow.evidence]
        lines += ["", "```yaml", workflow.yaml.strip(), "```"]
    return "\n".join(lines).strip() + "\n"
