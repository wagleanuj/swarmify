"""A workflow as YAML, from stage definitions. The inverse of `parse` for what a run recorded:
saving a finished run as a workflow turns the stages that actually happened back into text."""

from collections.abc import Mapping, Sequence
from typing import Any

import yaml


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
