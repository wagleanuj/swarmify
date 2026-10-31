"""The curated index (ADR 0019, issue 226): one TOML file in a public repository listing known
Connector Packages, where each is fetched from and the permissions it declares, so the
Connectors page can browse them. There is no marketplace: authors add a package by pull
request (see `docs/index-repo/README.md`). An entry's permissions are a summary for browsing;
installing it goes through the normal review, whose permissions, read from the clone, are what
the operator approves.

The format, one `[[packages]]` table per package (fields the format does not name are ignored,
so a newer index still reads):

    [[packages]]
    name = "speech"                    # the package's Manifest name
    description = "Speaks a script."   # one or two sentences
    git = "https://github.com/you/repo.git"
    subfolder = "connectors/speech"    # optional: where the package is in the repository
    ref = "v1.0.0"                     # the recommended tag, branch or commit
    permissions = ["network: api.example.org"]  # as the review lists them
"""

import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from jsonschema.exceptions import best_match
from jsonschema.validators import Draft202012Validator

INDEX_URL = "https://raw.githubusercontent.com/wagleanuj/swarmify-connectors/main/index.toml"
"""The default for the `SWARMIFY_CONNECTOR_INDEX` setting."""
MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 10.0

_TEXT = {"type": "string", "minLength": 1}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["packages"],
    "properties": {
        "packages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "description", "git", "ref", "permissions"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,59}$"},
                    "description": {**_TEXT, "maxLength": 1000},
                    "git": {**_TEXT, "maxLength": 1000},
                    "subfolder": {**_TEXT, "maxLength": 200},
                    "ref": {"type": "string", "pattern": "^(?!-)[A-Za-z0-9._/-]{1,200}$"},
                    "permissions": {"type": "array", "items": {**_TEXT, "maxLength": 400}},
                },
            },
        }
    },
}
FIELDS = ("name", "description", "git", "subfolder", "ref", "permissions")


class IndexUnavailable(RuntimeError):
    """The index could not be fetched or read; the message says why, for the operator."""


def _download(url: str, transport: httpx.BaseTransport | None) -> bytes:
    """The file at an https URL, at most MAX_BYTES within TIMEOUT_SECONDS; no redirects, so it
    stays https."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    body = b""
    try:
        with (
            httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise IndexUnavailable(f"it is over {MAX_BYTES // 1000} kB")
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("too slow")
    except httpx.HTTPStatusError as exc:
        raise IndexUnavailable(f"it answered {exc.response.status_code}") from exc
    except httpx.TimeoutException as exc:
        raise IndexUnavailable(f"it took over {TIMEOUT_SECONDS:g} s") from exc
    except httpx.HTTPError as exc:
        raise IndexUnavailable(f"it could not be reached ({exc})") from exc
    return body


def read_index(where: str, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """The entries of the index at `where`, an https URL or a local file path, each with the
    FIELDS (`subfolder` None when absent). Raises IndexUnavailable saying why it could not be
    fetched, or naming the first bad field (`packages[2].ref: ...`)."""
    scheme = urlsplit(where).scheme
    if scheme == "https":
        raw = _download(where, transport)
    elif "://" in where:
        raise IndexUnavailable(f"{scheme}:// is refused: the index is fetched over https:// only")
    else:
        path = Path(where).expanduser()
        if not path.is_file():
            raise IndexUnavailable(f"there is no file at {path}")
        if path.stat().st_size > MAX_BYTES:
            raise IndexUnavailable(f"it is over {MAX_BYTES // 1000} kB")
        raw = path.read_bytes()
    try:
        index = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise IndexUnavailable(f"it is not TOML: {exc}") from exc
    error = best_match(Draft202012Validator(SCHEMA).iter_errors(index))
    if error is not None:
        field = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        raise IndexUnavailable(f"{field.lstrip('.') or 'index'}: {error.message}")
    return [{f: entry.get(f) for f in FIELDS} for entry in index["packages"]]
