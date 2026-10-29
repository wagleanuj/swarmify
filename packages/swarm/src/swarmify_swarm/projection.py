"""What the Planner and the Judge answer, and how a passing Verdict becomes typed Artifacts.

Both are short sessions whose structured JSON output follows the schemas below. Parsing is pure
and strict about shape, forgiving about extras: a malformed answer raises ValueError, which
fails that session. Projection (ADR 0011) turns the judge's items into artifact files for the
stage's declared output type, each keeping only source references that point at a real
paragraph of one of the stage's notes.

A type a connector declares (issue 133) comes with its JSON Schema, which then replaces an
item's generic `fields`: the judge's structured output carries it, a parsed item is validated
against it, and the artifact carries it as a fenced JSON block for the connector.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for

MAX_LOOPS = 3
"""Gap loops a stage may take. The verdict after the third loop is final: fail fails the stage."""

_TYPE = re.compile(r"^\s*(list\[\s*([a-z][a-z0-9_]{0,39})\s*\]|([a-z][a-z0-9_]{0,39}))\s*$")

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "after": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Indexes of earlier tasks in this list it depends on",
                    },
                },
                "required": ["title"],
            },
        }
    },
    "required": ["tasks"],
}

_SOURCE = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "A note path exactly as listed"},
        "paragraph": {"type": "integer", "description": "The paragraph number as listed"},
    },
    "required": ["note", "paragraph"],
}
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "On fail: one task title per missing piece",
        },
        "items": {
            "type": "array",
            "description": "On pass: the stage's output as typed items",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "rank": {"type": "integer"},
                    "reason": {"type": "string", "description": "Why it has its rank, briefly"},
                    "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                    "sources": {"type": "array", "items": _SOURCE},
                },
                "required": ["title", "body", "sources"],
            },
        },
    },
    "required": ["passed", "reasons"],
}


def verdict_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """VERDICT_SCHEMA, or with a declared type's `schema` as each item's required `fields`."""
    if schema is None:
        return VERDICT_SCHEMA
    items = VERDICT_SCHEMA["properties"]["items"]
    item = items["items"]
    typed = {
        **item,
        "properties": {**item["properties"], "fields": schema},
        "required": [*item["required"], "fields"],
    }
    return {
        **VERDICT_SCHEMA,
        "properties": {**VERDICT_SCHEMA["properties"], "items": {**items, "items": typed}},
    }


@dataclass(frozen=True)
class OutputType:
    """`list[idea]` is many artifacts of type idea; `document` is one of type document."""

    type: str
    many: bool


def output_type(declared: str | None) -> OutputType | None:
    """The stage's `output_type`, or None when it declares none. Raises on a malformed one."""
    if declared is None or not declared.strip():
        return None
    m = _TYPE.match(declared)
    if not m:
        raise ValueError(f"{declared!r} is not an output type like idea or list[idea]")
    return OutputType(m.group(2), True) if m.group(2) else OutputType(m.group(3), False)


@dataclass(frozen=True)
class SeedTask:
    title: str
    body: str = ""
    after: tuple[int, ...] = ()
    """Indexes of earlier seed tasks this one depends on."""


def parse_plan(output: Any) -> list[SeedTask]:
    tasks = _dict(output).get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("the planner returned no tasks")
    seeds: list[SeedTask] = []
    for i, raw in enumerate(tasks):
        task = _dict(raw)
        title = str(task.get("title") or "").strip()
        if not title:
            raise ValueError(f"seed task {i} has no title")
        after = tuple(
            dict.fromkeys(a for a in task.get("after") or [] if isinstance(a, int) and 0 <= a < i)
        )
        seeds.append(SeedTask(title, str(task.get("body") or ""), after))
    return seeds


@dataclass(frozen=True)
class Source:
    note: str
    paragraph: int


@dataclass(frozen=True)
class Item:
    title: str
    body: str
    rank: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    """Strings by name, or for a declared type the value its schema describes."""
    sources: list[Source] = field(default_factory=list)
    reason: str = ""
    """Why the judge gave it its rank: what a pick gate shows beside the rank."""


@dataclass(frozen=True)
class Judgement:
    passed: bool
    reasons: list[str]
    gaps: list[str]
    items: list[Item]


def parse_verdict(output: Any, schema: dict[str, Any] | None = None) -> Judgement:
    """`schema`, a declared type's, is what each passing item's `fields` must be: one that is
    not raises ValueError, naming where it is wrong."""
    data = _dict(output)
    if not isinstance(data.get("passed"), bool):
        raise ValueError("the judge returned no pass or fail")
    reasons = [str(r).strip() for r in data.get("reasons") or [] if str(r).strip()]
    if not reasons:
        raise ValueError("the judge gave no reasons")
    passed = data["passed"]
    gaps = [str(g).strip() for g in data.get("gaps") or [] if str(g).strip()]
    if not passed and not gaps:
        # A fail must leave work on the board, or the next loop would judge the same thing.
        gaps = [f"Close the gap: {r}" for r in reasons]
    items = []
    for n, raw in enumerate((data.get("items") or []) if passed else [], 1):
        item = _dict(raw)
        rank = item.get("rank")
        if schema is None:
            fields = {str(k): str(v) for k, v in _dict(item.get("fields") or {}).items()}
        else:
            fields = item.get("fields")
            wrong = best_match(validator_for(schema)(schema).iter_errors(fields))
            if wrong is not None:
                at = "/".join(str(p) for p in wrong.absolute_path) or "the top"
                raise ValueError(f"item {n} does not fit its type at {at}: {wrong.message}")
            fields = _dict(fields)
        items.append(
            Item(
                str(item.get("title") or "").strip() or "Untitled",
                str(item.get("body") or "").strip(),
                rank if isinstance(rank, int) else None,
                fields,
                [
                    Source(str(s.get("note")), s["paragraph"])
                    for s in (_dict(x) for x in item.get("sources") or [])
                    if isinstance(s.get("paragraph"), int)
                ],
                str(item.get("reason") or "").strip(),
            )
        )
    return Judgement(passed, reasons, [] if passed else gaps, items)


def paragraphs(body: str) -> list[str]:
    """A note's paragraphs as the judge sees them numbered from 1: blocks between blank lines."""
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def notes_for_judge(notes: list[tuple[str, str]]) -> str:
    """The stage's notes with numbered paragraphs, so the judge can cite them."""
    out = []
    for path, body in notes:
        out.append(f"## {path}")
        out += [f"[{n}] {p}" for n, p in enumerate(paragraphs(body), 1)]
    return "\n".join(out) or "(no notes were written)"


@dataclass(frozen=True)
class Projected:
    """One artifact file to write: `name` in the stage's artifacts folder."""

    name: str
    type: str
    fields: dict[str, Any]
    body: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "item"


def project(
    kind: OutputType,
    items: list[Item],
    notes: list[tuple[str, str]],
    declared: bool = False,
    produces: list[str] | None = None,
) -> list[Projected]:
    """Typed artifacts from the judge's items. A source is kept only when its note is one of the
    stage's notes and its paragraph exists; it is written as `<note path>#p<n>` with the
    paragraph quoted in the body. A single output type keeps only the best ranked item.

    A `declared` type's fields go in the body as a fenced JSON block, before the readable body,
    not in the frontmatter. A single one is written as the stage's produced markdown file (the
    one named after the type, else the first), so the connector reading that file gets it."""
    if not kind.many:
        items = sorted(items, key=lambda i: i.rank if i.rank is not None else 1 << 30)[:1]
    paras = {path: paragraphs(body) for path, body in notes}
    md = [p for p in produces or [] if p.endswith(".md")]
    target = f"{kind.type}.md" if f"{kind.type}.md" in md else md[0] if md else None
    taken: set[str] = set()
    out: list[Projected] = []
    for item in items:
        name, n = f"{kind.type}-{_slug(item.title)}.md", 1
        if declared and not kind.many and target:
            name = target
        while name in taken:
            n += 1
            name = f"{kind.type}-{_slug(item.title)}-{n}.md"
        taken.add(name)
        refs = [
            s for s in item.sources if s.note in paras and 1 <= s.paragraph <= len(paras[s.note])
        ]
        # A declared type's top-level text also goes in the frontmatter, where readers such as
        # a publish stage look for title and description; nested fields stay in the JSON block.
        text = {k: v for k, v in item.fields.items() if isinstance(v, str)} if declared else {}
        fields: dict[str, Any] = {**(text if declared else item.fields), "title": item.title}
        if item.rank is not None:
            fields["rank"] = item.rank
        if item.reason:
            fields["reason"] = item.reason
        fields["sources"] = [f"{s.note}#p{s.paragraph}" for s in refs]
        lines = [f"# {item.title}", ""]
        if declared:
            lines += ["```json", json.dumps(item.fields, indent=2, ensure_ascii=False), "```", ""]
        lines.append(item.body)
        if not declared:
            lines += [f"\n**{k}**: {v}" for k, v in item.fields.items()]
        if refs:
            lines += ["", "## Sources", ""]
            lines += [
                f"- {s.note} paragraph {s.paragraph}: {_quote(paras[s.note][s.paragraph - 1])}"
                for s in refs
            ]
        out.append(Projected(name, kind.type, fields, "\n".join(lines).strip() + "\n"))
    return out


def verdict_file(loop: int, j: Judgement) -> Projected:
    """The verdict artifact of one judge pass, `verdict-<loop>.md`."""
    outcome = "pass" if j.passed else "fail"
    lines = [f"# Verdict {loop}: {outcome}", "", "## Reasons", ""] + [f"- {r}" for r in j.reasons]
    if j.gaps:
        lines += ["", "## Gap tasks", ""] + [f"- {g}" for g in j.gaps]
    fields = {
        "title": f"Verdict {loop}: {outcome}",
        "passed": j.passed,
        "loop": loop,
        "reasons": j.reasons,
        "gaps": j.gaps,
    }
    return Projected(f"verdict-{loop}.md", "verdict", fields, "\n".join(lines) + "\n")


def _quote(text: str) -> str:
    one = " ".join(text.split())
    return f'"{one[:200]}{"..." if len(one) > 200 else ""}"'


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    return value
