"""Shared SSE parsing for the tests that read a world's event stream."""

import json
from collections.abc import Iterator

import httpx


def sse_events(response: httpx.Response) -> Iterator[tuple[str, int, dict]]:
    """Yield (type, id, data) per SSE message, skipping keepalive comments."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if fields:
                yield fields["event"], int(fields["id"]), json.loads(fields["data"])
                fields = {}
            continue
        key, _, value = line.partition(": ")
        fields[key] = value
