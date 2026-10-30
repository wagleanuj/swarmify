"""Setup (ADR 0024): the values a Connector Package's `[[config]]` fields hold, and how they
reach its process.

Credentials (secret fields) and Settings live together in one JSON file in the data directory,
`setup.json`, mode 600, as `{package: {"shared": {key: value}, "worlds": {world: {key: value}}}}`.
Values are keyed by package, not version, so they outlive an upgrade; a field's `renamed_from`
finds a value saved under its old key. For a world, a field resolves to the world's value, else
the shared one (a `world`-scoped field has none), else its default. The process gets each
resolved field as the environment variable its `env` names, the upper-cased key by default.
An `oauth` field's value is its token set (see `oauth`); the process gets a fresh access token.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from swarmify_connectors.oauth import client_fields

Values = dict[str, Any]


def fields(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The Manifest's `[[config]]` fields in order, each `oauth` field after its app's client
    id and secret."""
    out = []
    for f in manifest.get("config", []):
        if f["type"] == "oauth":
            out += client_fields(f)
        out.append(f)
    return out


def secret(field: dict[str, Any]) -> bool:
    """A Credential: never returned through the API. A token set always is one."""
    return bool(field.get("secret")) or field["type"] == "oauth"


def needs_sign_in(field: dict[str, Any], value: Any) -> bool:
    """An oauth field whose token set the provider refused."""
    return field["type"] == "oauth" and isinstance(value, dict) and bool(value.get("needs_sign_in"))


def scope(field: dict[str, Any]) -> str:
    return str(field.get("scope", "shared"))


def env_name(field: dict[str, Any]) -> str:
    return str(field.get("env", field["key"].upper()))


def stored(field: dict[str, Any], values: Values) -> Any:
    """The field's value in one scope's values, under its key or the key it was renamed from."""
    if field["key"] in values:
        return values[field["key"]]
    return values.get(field.get("renamed_from", ""))


def coerce(field: dict[str, Any], value: Any) -> Any:
    """The value as its field's type, or None for an empty one. Raises ValueError naming the
    field's label."""
    if value is None or value == "":
        return None
    kind, label = field["type"], field["label"]

    def bad(why: str) -> ValueError:
        return ValueError(f"{label}: {why}")

    if kind == "oauth":
        raise bad("is set by signing in, not typed")

    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise bad("is yes or no")
    if kind in ("number", "integer"):
        try:
            if isinstance(value, bool):
                raise ValueError
            number = int(value) if kind == "integer" else float(value)
            if kind == "integer" and isinstance(value, float) and value != number:
                raise ValueError
        except ValueError:
            raise bad("is a whole number" if kind == "integer" else "is a number") from None
        if "min" in field and number < field["min"]:
            raise bad(f"is at least {field['min']}")
        if "max" in field and number > field["max"]:
            raise bad(f"is at most {field['max']}")
        return number
    if not isinstance(value, str):
        raise bad("is text")
    if kind == "choice" and value not in field["options"]:
        raise bad(f"is one of {', '.join(field['options'])}")
    if kind == "url" and not re.fullmatch(r"https?://\S+", value):
        raise bad("is an http(s) URL")
    if kind != "text" and ("\n" in value or "\r" in value):
        raise bad("is one line")
    if "pattern" in field and not re.fullmatch(field["pattern"], value):
        raise bad(f"does not match {field['pattern']}")
    return value


def resolve(manifest: dict[str, Any], shared: Values, world: Values) -> Values:
    """Each field's value for one world: the world's, else the shared one, else its default.
    Fields with none are left out."""
    out: Values = {}
    for f in fields(manifest):
        value = stored(f, world)
        if value is None and scope(f) == "shared":
            value = stored(f, shared)
        if value is None:
            value = f.get("default")
        if value is not None:
            out[f["key"]] = value
    return out


def missing(manifest: dict[str, Any], resolved: Values) -> list[str]:
    """Keys of required fields that resolve to nothing, or to a sign-in to do again."""
    return [
        f["key"]
        for f in fields(manifest)
        if f.get("required") and (f["key"] not in resolved or needs_sign_in(f, resolved[f["key"]]))
    ]


def environment(manifest: dict[str, Any], resolved: Values) -> dict[str, str]:
    """The resolved values as the process environment they reach it as. An oauth field reaches
    it only once its token set is replaced by the access token; its app's client never does."""
    out = {}
    for f in fields(manifest):
        if f["key"] in resolved and "client_of" not in f:
            value = resolved[f["key"]]
            if isinstance(value, dict):
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            out[env_name(f)] = str(value)
    return out


class SetupStore:
    """The setup file. Refuses one that group or others can read, as the credentials file does."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{self.path} is mode {mode:o}; run `chmod 600 {self.path}` so only you can read it"
            )
        return dict(json.loads(self.path.read_text()))

    def values(self, package: str, world_id: str) -> tuple[Values, Values]:
        """The package's shared values and the world's own."""
        saved = self.read().get(package, {})
        return dict(saved.get("shared", {})), dict(saved.get("worlds", {}).get(world_id, {}))

    def put(self, package: str, world_id: str, shared: Values, world: Values) -> None:
        """Replace the package's shared values and the world's own."""
        # ponytail: read-modify-write without a lock; two saves at once can lose one.
        data = self.read()
        saved = data.setdefault(package, {})
        saved["shared"] = shared
        saved.setdefault("worlds", {})[world_id] = world
        self._write(data)

    def put_value(self, package: str, world_id: str | None, key: str, value: Any) -> None:
        """Set one value, the shared one when `world_id` is None, as a sign-in does."""
        data = self.read()
        saved = data.setdefault(package, {})
        where = saved.setdefault("shared", {})
        if world_id is not None:
            where = saved.setdefault("worlds", {}).setdefault(world_id, {})
        where[key] = value
        self._write(data)

    def delete_world(self, world_id: str) -> None:
        data = self.read()
        if any(world_id in p.get("worlds", {}) for p in data.values()):
            for p in data.values():
                p.get("worlds", {}).pop(world_id, None)
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)  # O_CREAT's mode is masked by umask and ignored for an existing tmp
        os.replace(tmp, self.path)
