"""Connector Packages (ADRs 0019, 0023, 0026): the Manifest and installing a package.

A Connector Package is a folder holding a `swarmify-connector.toml` Manifest and the connector's
code. `read_manifest` reads one and checks it against `SCHEMA` plus the rules a JSON Schema
cannot say, raising ManifestError naming the field. Installing copies the package to
`<data dir>/packages/<name>/<version>/` and runs its `install` step there, so its dependencies
land beside it; the install is recorded once for the whole install by `.swarmify-install.json`,
written last, so a version folder without it never counts and a failed step removes the folder.
"""

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jsonschema.exceptions import SchemaError, ValidationError, best_match
from jsonschema.validators import Draft202012Validator, validator_for

from swarmify_connectors.model import KINDS, STAGE_TYPES

MANIFEST = "swarmify-connector.toml"
RECORD = ".swarmify-install.json"
INSTALL_SECONDS = 900
COPY_SKIPS = shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__", RECORD)
CAPABILITIES = ("publish", "outcomes", "build_session", "check", "remote_approvals")
FIELD_TYPES = ("string", "text", "number", "integer", "boolean", "choice", "url", "path", "oauth")
_PYTHON = {"string": str, "text": str, "url": str, "path": str, "choice": str}
_PYTHON |= {"number": (int, float), "integer": int, "boolean": bool}

_SNAKE = "^[a-z][a-z0-9_]{0,59}$"
_TOOL = "^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
_URL = "^https?://[^\\s/]+"
_VERSION = "^\\d+\\.\\d+\\.\\d+([-+][0-9A-Za-z.-]+)?$"
_strings = {"type": "array", "items": {"type": "string", "minLength": 1}}
_text = {"type": "string", "minLength": 1}
_file = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "media_type"],
    "properties": {
        "description": _text,
        "media_type": {"type": "string", "pattern": "^[a-z]+/([a-z0-9.+-]+|\\*)$"},
    },
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "label", "about", "kind", "stage_types", "runtime"],
    "properties": {
        # What it is
        "name": {"type": "string", "pattern": _SNAKE},
        "version": {"type": "string", "pattern": _VERSION},
        "label": {"type": "string", "minLength": 1, "maxLength": 60},
        "about": _text,
        "kind": {"enum": list(KINDS)},
        "stage_types": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"enum": list(STAGE_TYPES)},
        },
        # Planning
        "params": {"type": "object"},
        "planner_notes": _text,
        "inputs": {"type": "array", "items": _file},
        "outputs": {"type": "array", "items": _file},
        "types": {
            "type": "object",
            "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,39}$"},
            "additionalProperties": {"type": "object"},
        },
        "input_types": {"type": "array", "items": {"type": "string"}},
        "world_types": {"type": "string", "pattern": _TOOL},
        # Running
        "runtime": {"enum": ["uv", "npm", "container"]},
        "command": {**_strings, "minItems": 1},
        "url": {"type": "string", "pattern": _URL},
        "install": {**_strings, "minItems": 1},
        "health": {"type": "string", "pattern": _TOOL},
        # Setup (ADR 0024)
        "setup": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "test": {"type": "string", "pattern": _TOOL},
                "help_url": {"type": "string", "pattern": _URL},
            },
        },
        "config": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "type"],
                "properties": {
                    "key": {"type": "string", "pattern": _SNAKE},
                    "label": {"type": "string", "minLength": 1, "maxLength": 60},
                    "type": {"enum": list(FIELD_TYPES)},
                    "secret": {"type": "boolean"},
                    "required": {"type": "boolean"},
                    "default": {"type": ["string", "number", "boolean"]},
                    "scope": {"enum": ["shared", "world"]},
                    "env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
                    "help": _text,
                    "options": {**_strings, "minItems": 1, "uniqueItems": True},
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "pattern": _text,
                    "renamed_from": {"type": "string", "pattern": _SNAKE},
                    "oauth": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["authorize_url", "token_url"],
                        "properties": {
                            "authorize_url": {"type": "string", "pattern": _URL},
                            "token_url": {"type": "string", "pattern": _URL},
                            "scopes": _strings,
                            "pkce": {"type": "boolean"},
                            "client": {"enum": ["operator"]},
                        },
                    },
                },
            },
        },
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "url"],
                "properties": {
                    "name": {"type": "string", "pattern": _SNAKE},
                    "url": {"type": "string", "pattern": _SNAKE},
                    "health": {"type": "string", "pattern": "^/"},
                    "provision": {"type": "string", "pattern": _TOOL},
                },
            },
        },
        "owned_folders": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,59}$"},
        },
        "capabilities": {"type": "array", "uniqueItems": True, "items": {"enum": CAPABILITIES}},
        "primary_metric": {"type": "string", "pattern": _SNAKE},
        "permissions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "network": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^(\\*\\.)?[a-z0-9.-]+(:\\d+)?$"},
                },
                "services": _strings,
                "folders": _strings,
            },
        },
    },
}
"""The Manifest as a JSON Schema: every section, including those later tickets act on, so an
author hears of a mistake at install rather than when the feature lands."""


class ManifestError(ValueError):
    """An invalid Manifest; the message starts with the field, as `config[1].options: ...`."""


class InstallError(RuntimeError):
    """A package that could not be installed; nothing of it is left behind."""


@dataclass(frozen=True)
class Package:
    """One installed version of a Connector Package. `state` is `installed`, or `invalid` with
    `problem` when its Manifest no longer passes (a newer Swarmify, an edited file)."""

    name: str
    version: str
    label: str
    kind: str
    about: str
    state: str
    source: str
    installed_at: str
    problem: str | None = None


def _where(path: Any) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)
    return out or "manifest"


def _schema_error(error: ValidationError) -> ManifestError:
    path, instance = list(error.absolute_path), cast(dict[str, Any], error.instance)
    schema = cast(dict[str, Any], error.schema)
    if error.validator == "required":
        missing = next(n for n in cast(list[str], error.validator_value) if n not in instance)
        return ManifestError(f"{_where([*path, missing])}: is required")
    if error.validator == "additionalProperties":
        extra = sorted(set(instance) - set(schema.get("properties", {})))
        return ManifestError(f"{_where([*path, extra[0]])}: is not a Manifest field")
    return ManifestError(f"{_where(path)}: {error.message}")


def _is_schema(schema: dict[str, Any]) -> str | None:
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        return exc.message
    return None


def check_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The Manifest when it is valid. Raises ManifestError naming the first bad field."""
    error = best_match(Draft202012Validator(SCHEMA).iter_errors(manifest))
    if error is not None:
        raise _schema_error(error)

    def bad(field: str, why: str) -> None:
        raise ManifestError(f"{field}: {why}")

    if ("command" in manifest) == ("url" in manifest):
        bad("command", "give the command that starts its MCP server, or its url, not both")
    if manifest["kind"] == "publish" and manifest["stage_types"] != ["connector"]:
        bad("stage_types", "a publish connector runs only in connector stages, after a gate")
    if "params" in manifest and (why := _is_schema(manifest["params"])):
        bad("params", f"is not a JSON Schema: {why}")
    types = manifest.get("types", {})
    for name, schema in types.items():
        if why := _is_schema(schema):
            bad(f"types.{name}", f"is not a JSON Schema: {why}")
    for i, name in enumerate(manifest.get("input_types", [])):
        if name and name not in types:
            bad(f"input_types[{i}]", f"{name} is not one of its types")
    if "world_types" in manifest and not types:
        bad("world_types", "answers declared types: declare types first")
    keys: dict[str, dict[str, Any]] = {}
    envs: set[str] = set()
    for i, field in enumerate(manifest.get("config", [])):
        at, kind = f"config[{i}]", field["type"]
        if field["key"] in keys:
            bad(f"{at}.key", f"{field['key']} is declared twice")
        keys[field["key"]] = field
        env = field.get("env", field["key"].upper())
        if env in envs:
            bad(f"{at}.env", f"{env} is used by two fields")
        envs.add(env)
        if (kind == "choice") != ("options" in field):
            bad(f"{at}.options", "a choice field lists its options, and only a choice field")
        if (kind == "oauth") != ("oauth" in field):
            bad(f"{at}.oauth", "an oauth field declares [config.oauth], and only an oauth field")
        if "default" in field:
            value = field["default"]
            if kind == "oauth":
                bad(f"{at}.default", "an oauth field has no default")
            wrong = isinstance(value, bool) != (kind == "boolean")
            if wrong or not isinstance(value, _PYTHON[kind]):
                bad(f"{at}.default", f"is not of its type, {kind}")
            if kind == "choice" and value not in field["options"]:
                bad(f"{at}.default", f"{value} is not one of its options")
    names = set()
    for i, service in enumerate(manifest.get("services", [])):
        if service["url"] not in keys:
            bad(f"services[{i}].url", f"{service['url']} is not a [[config]] key")
        names.add(service["name"])
    for i, name in enumerate(manifest.get("permissions", {}).get("services", [])):
        if name not in names:
            bad(f"permissions.services[{i}]", f"{name} is not one of its [[services]]")
    if "primary_metric" in manifest and "outcomes" not in manifest.get("capabilities", []):
        bad("primary_metric", "names an outcome, so the package offers the outcomes capability")
    return manifest


def read_manifest(folder: Path) -> dict[str, Any]:
    """The checked Manifest at the root of a package folder. Raises ManifestError."""
    file = folder / MANIFEST
    if not file.is_file():
        raise ManifestError(f"manifest: {folder} has no {MANIFEST}")
    try:
        manifest = tomllib.loads(file.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest: {MANIFEST} is not TOML: {exc}") from exc
    return check_manifest(manifest)


def _install_env() -> dict[str, str]:
    """What the install step sees: enough to find uv, npm and their caches, and none of the
    app's own variables (no keys, no VIRTUAL_ENV pointing at Swarmify's venv)."""
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


class Packages:
    """Connector Packages installed for the whole install under `root` (`<data dir>/packages`),
    from a folder of this repository (`repo`), as `connectors/speech`, or a local path."""

    def __init__(self, root: Path, repo: Path) -> None:
        self.root = root
        self.repo = repo

    def list(self) -> list[Package]:
        """Every installed version, by name then version folder."""
        return [self._package(f) for f in sorted(self.root.glob(f"*/*/{RECORD}"))]

    def manifest(self, name: str, version: str) -> dict[str, Any]:
        """The Manifest of an installed version. Raises InstallError when it is not installed,
        ManifestError when its Manifest no longer passes."""
        installed = re.fullmatch(_SNAKE, name) and re.fullmatch(_VERSION, version)
        folder = self.root / name / version
        if not installed or not (folder / RECORD).is_file():
            raise InstallError(f"{name} {version} is not installed")
        return read_manifest(folder)

    def _package(self, record_file: Path) -> Package:
        folder, record = record_file.parent, json.loads(record_file.read_text())
        name, version = folder.parent.name, folder.name
        try:
            m = read_manifest(folder)
        except ManifestError as exc:
            return Package(name, version, name, "", "", "invalid", **record, problem=str(exc))
        return Package(name, version, m["label"], m["kind"], m["about"], "installed", **record)

    def _source(self, source: str) -> Path:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = (self.repo / path).resolve()
            if not path.is_relative_to(self.repo.resolve()):
                raise InstallError(f"{source} is outside this repository")
        if not path.is_dir():
            raise InstallError(f"no package folder at {path}")
        return path

    def install(self, source: str) -> Package:
        """Copy the package at `source` to `<root>/<name>/<version>/` and run its install step
        there. Raises ManifestError for a bad Manifest, InstallError when the version is
        installed already or the step fails; either way nothing new is left behind."""
        folder = self._source(source)
        manifest = read_manifest(folder)
        target = self.root / manifest["name"] / manifest["version"]
        if (target / RECORD).exists():
            raise InstallError(f"{manifest['name']} {manifest['version']} is already installed")
        # ponytail: no lock; two installs of one version at once race. Add one if the UI can.
        shutil.rmtree(target, ignore_errors=True)  # what a crash mid-install left
        try:
            shutil.copytree(folder, target, ignore=COPY_SKIPS)
            if manifest.get("install"):
                self._run_install(manifest, target)
            record = {"source": source, "installed_at": datetime.now(UTC).isoformat()}
            (target / f"{RECORD}.tmp").write_text(json.dumps(record))
            (target / f"{RECORD}.tmp").replace(target / RECORD)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise
        return self._package(target / RECORD)

    @staticmethod
    def _run_install(manifest: dict[str, Any], target: Path) -> None:
        step = manifest["install"]
        try:
            done = subprocess.run(
                step,
                cwd=target,
                env=_install_env(),
                capture_output=True,
                text=True,
                timeout=INSTALL_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError(f"{manifest['name']}'s install step did not run: {exc}") from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout).strip()[-600:]
            raise InstallError(
                f"{manifest['name']}'s install step `{' '.join(step)}` failed"
                f" ({done.returncode}): {tail}"
            )
