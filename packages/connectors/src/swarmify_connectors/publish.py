"""Publish stages (ADR 0007): the core holds the gate, the connector says what goes out.

A publish connector answers two calls. `preview` gets `inputs` (the stage's input file names),
`params` (the stage's, less `metadata`), `metadata` (the frontmatter of the run file
`params.metadata` names, else {}) and `edits` (the gate's, by field key), and answers
`{fields: [{key, label, value, editable, hint}], problems: [str]}` with the edits applied to its
editable fields. `publish` gets `fields` (every value as confirmed, by key), `idempotency_key`
and `input_path`/`input_paths`, posts exactly those values, and answers `url` or `links`
(platform to URL), and optionally `id` and `status` (published, scheduled). The core shows the
fields at the gate, lets the person edit the editable ones, refuses to go on while there are
problems, and records the values confirmed. A world's own publish connector answers both as MCP
tools; the built-ins answer in process (`builtin_publish`) until they move into packages (#111).
"""

import re
from typing import Any

from swarmify_connectors import builtin_publish


def answer(raw: Any) -> dict[str, Any]:
    """A connector's `preview` answer, checked: well-formed fields in, anything else a problem."""
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), list):
        return {"fields": [], "problems": [f"preview answered without fields: {raw!r}"[:300]]}
    fields, problems = [], [str(p) for p in raw.get("problems") or []]
    for f in raw["fields"]:
        if isinstance(f, dict) and isinstance(f.get("key"), str) and f["key"]:
            fields.append(
                {
                    "key": f["key"],
                    "label": str(f.get("label") or f["key"]),
                    "value": f.get("value"),
                    "editable": f.get("editable") is True,
                    "hint": str(f.get("hint") or ""),
                }
            )
        else:
            problems.append(f"preview answered a field without a key: {f!r}"[:300])
    return {"fields": fields, "problems": problems}


def result(connector: str, response: dict[str, Any]) -> dict[str, Any]:
    """What came back, normalized: `status` (published, scheduled or pending), `links` (platform
    to URL), `id` (the post, scheduler job or background request) and `failed` (platform to
    error)."""
    if connector in builtin_publish.NAMES:
        return builtin_publish.result(connector, response)
    links = response.get("links")
    links = {str(k): str(v) for k, v in links.items()} if isinstance(links, dict) else {}
    if response.get("url"):
        links.setdefault(connector, str(response["url"]))
    status = "scheduled" if response.get("status") == "scheduled" else "published"
    return {"status": status, "links": links, "id": response.get("id"), "failed": {}}


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return "" if value is None else str(value)


def markdown(connector: str, fields: list[dict[str, Any]], out: dict[str, Any]) -> str:
    """The publish artifact body: what went out and where it landed. The links are in the body
    because the graph reads bodies, not frontmatter."""
    values = {f["key"]: f["value"] for f in fields}
    title = str(values.get("title") or f"Posted to {connector}")
    sent = [f"- {f['label']}: {_shown(f['value']) or f['hint'] or 'none'}" for f in fields]
    if out["links"]:
        landed = [f"- {p}: {url}" for p, url in out["links"].items()]
    else:
        landed = [f"- no link yet: {connector} job {out['id']} is {out['status']}"]
    landed += [f"- {p} failed: {error}" for p, error in out["failed"].items()]
    return (
        f"# {title}\n\nSent to {connector}:\n\n"
        + "\n".join(sent)
        + "\n\nWhere it landed:\n\n"
        + "\n".join(landed)
        + "\n"
    )


_YOUTUBE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/))([\w-]{6,20})"
)


def youtube_ids(fields: dict[str, Any]) -> list[str]:
    """The YouTube video ids a publish artifact names: every YouTube link, whichever connector
    posted it, plus the video id of a `youtube` post that has no link yet."""
    links = fields.get("links")
    links = links if isinstance(links, dict) else {}
    ids = [m[1] for url in links.values() if (m := _YOUTUBE.match(str(url)))]
    if fields.get("connector") == "youtube" and fields.get("id"):
        ids.append(str(fields["id"]))
    return list(dict.fromkeys(ids))
