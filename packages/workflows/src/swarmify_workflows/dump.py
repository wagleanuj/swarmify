"""A workflow as YAML, from stage definitions. The inverse of `parse` for what a run recorded:
saving a finished run as a workflow turns the stages that actually happened back into text."""

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from swarmify_workflows.model import STAGE_TYPES
from swarmify_workflows.parse import _Loader


def dump_workflow(
    name: str,
    stages: Sequence[tuple[str, str, Mapping[str, Any]]],
    *,
    schedule: str | None = None,
    budget_usd: float | None = None,
    agents: int | None = None,
) -> str:
    """`stages` are (type, name, fields). Empty fields are left out, so the text stays as short
    as a person would write it; the parser fills the defaults back in."""
    doc: dict[str, Any] = {"name": name}
    for key, value in (("schedule", schedule), ("budget_usd", budget_usd), ("agents", agents)):
        if value is not None:
            doc[key] = value
    doc["stages"] = [
        {kind: stage_name, **{k: v for k, v in fields.items() if v not in (None, [], {}, "")}}
        for kind, stage_name, fields in stages
    ]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def restage(
    text: str, before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> tuple[str, str] | None:
    """A workflow's YAML with the stages `before` (each a `name`, `type` and fields) given way,
    where the first of them stands, to the stages `after`, matched by name. A field the change
    left as it was keeps the text's own value, so a newer edit to it stays. Returns the text as
    it was and as it will be, both written the same way so they compare line by line, or None
    when the text has no stage named as the first of `before`.

    ponytail: written back through YAML, so the text's comments and quoting go; edit the text
    itself if operators keep comments in their workflows."""
    doc = yaml.load(text, _Loader)
    entries: list[dict[str, Any]] = doc["stages"]
    named = {_name(e): e for e in entries}
    if not before or before[0]["name"] not in named:
        return None
    gone = {s["name"] for s in before}
    old = {s["name"]: s for s in before}
    at = sum(1 for e in entries[: entries.index(named[before[0]["name"]])] if _name(e) not in gone)
    kept = [e for e in entries if _name(e) not in gone]
    new = [_entry(named.get(s["name"]), old.get(s["name"], {}), s) for s in after]
    return _dump(doc), _dump({**doc, "stages": [*kept[:at], *new, *kept[at:]]})


def _name(entry: Mapping[str, Any]) -> str:
    return next(entry[t] for t in STAGE_TYPES if t in entry)


def _entry(
    entry: Mapping[str, Any] | None, old: Mapping[str, Any], new: Mapping[str, Any]
) -> dict[str, Any]:
    kind, name = new["type"], new["name"]
    if not entry or entry.get(kind) != name:
        entry, old = {kind: name}, {}
    out = dict(entry)
    fields = {k: v for k, v in new.items() if k not in ("name", "type")}
    for key in {*old, *fields} - {"name", "type"}:
        value = fields.get(key)
        if key in old and old[key] == value:
            continue
        if value in (None, [], {}, ""):
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _dump(doc: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=100)
