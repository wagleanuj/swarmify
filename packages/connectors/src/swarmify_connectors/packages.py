"""Connector Packages (ADRs 0019, 0023, 0026): the Manifest and installing a package.

A Connector Package is a folder holding a `swarmify-connector.toml` Manifest and the connector's
code. `read_manifest` reads one and checks it against `SCHEMA` plus the rules a JSON Schema
cannot say, raising ManifestError naming the field. Installing copies the package to
`<data dir>/packages/<name>/<version>/` and runs its `install` step there, so its dependencies
land beside it; the install is recorded once for the whole install by `.swarmify-install.json`,
written last, so a version folder without it never counts and a failed step removes the folder.
A package also installs from a git URL at a ref (issue 225): the clone runs nothing of the
package, and the API installs only once the operator approved every permission it declares.
A local package is one Swarmify wrote the Manifest of itself, from the Connectors page's "add an
MCP server" form (issue 211); it has no code of its own, only the command or URL it names.
"""

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from jsonschema.exceptions import SchemaError, ValidationError, best_match
from jsonschema.validators import Draft202012Validator, validator_for

from swarmify_connectors import setup
from swarmify_connectors.model import KINDS, STAGE_TYPES
from swarmify_connectors.sandbox import REGISTRIES, Grants, Sandbox, domain

log = logging.getLogger(__name__)

MANIFEST = "swarmify-connector.toml"
RECORD = ".swarmify-install.json"
LOCAL = "local"
"""The install source recorded for a local package."""
INSTALL_SECONDS = 900
GIT_SECONDS = 300
_REF = re.compile(r"^(?!-)[A-Za-z0-9._/-]{1,200}$")
"""A tag, branch or commit to check out: never an option, never git's revision syntax."""
_SUBFOLDER = re.compile(r"^(?!.*(^|/)\.\.?(/|$))[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
"""A folder of a repository, as `connectors/speech`: relative, never `.` or `..`."""
SKIPPED = (".git", ".venv", "node_modules", "__pycache__")
COPY_SKIPS = shutil.ignore_patterns(*SKIPPED, RECORD)
CAPABILITIES = ("publish", "outcomes", "build_session", "check", "remote_approvals")
FIELD_TYPES = ("string", "text", "number", "integer", "boolean", "choice", "url", "path", "oauth")
_PYTHON = {"string": str, "text": str, "url": str, "path": str, "choice": str}
_PYTHON |= {"number": (int, float), "integer": int, "boolean": bool}

_SNAKE = "^[a-z][a-z0-9_]{0,59}$"
_TOOL = "^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
_URL = "^https?://[^\\s/]+"
_VERSION = "^\\d+\\.\\d+\\.\\d+([-+][0-9A-Za-z.-]+)?$"
_MEDIA = "^[a-z]+/([a-z0-9.+-]+|\\*)$"
_strings = {"type": "array", "items": {"type": "string", "minLength": 1}}
_text = {"type": "string", "minLength": 1}
_domains = {
    "type": "array",
    "items": {"type": "string", "pattern": "^(\\*\\.)?[a-z0-9.-]+(:\\d+)?$"},
}
_FOLDER = "^(\\{world\\}|[a-z0-9][a-z0-9_-]*)(/(\\{world\\}|[a-z0-9][a-z0-9_-]*))*$"
"""A folder of the data directory: plain names, so never a file such as `setup.json`."""
_inside = {"type": "string", "pattern": "^(?![/~])(?!.*(^|/)\\.\\.(/|$)).+$"}
"""A path inside the package folder, relative to it."""
_file = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "media_type"],
    "properties": {
        "description": _text,
        "media_type": {"type": "string", "pattern": _MEDIA},
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
        # A remote_approvals connector's long-lived process, which the core keeps running while a
        # world enables it with its Setup done (a chat app's socket connection, say).
        "listen": {**_strings, "minItems": 1},
        # False keeps one of this repository's packages installed but off until a world enables it.
        "enable_by_default": {"type": "boolean"},
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
                            "authorize_params": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
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
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["folder", "label"],
                "properties": {
                    "folder": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,59}$"},
                    "label": {"type": "string", "minLength": 1, "maxLength": 60},
                    "about": _text,
                    "accept": {**_strings, "items": {"type": "string", "pattern": _MEDIA}},
                    "confirm": _text,
                },
            },
        },
        "capabilities": {"type": "array", "uniqueItems": True, "items": {"enum": CAPABILITIES}},
        # The Build Session Capability (ADR 0018): the core runs one sandboxed coding session in
        # a build folder seeded from `starter`, then calls `tool` on the result as the stage's
        # call. The session's network is `permissions.network`.
        "build_session": {
            "type": "object",
            "additionalProperties": False,
            "required": ["system", "brief", "starter", "tool"],
            "properties": {
                "system": _inside,
                "brief": _inside,
                "starter": _inside,
                "notes": {"type": "object", "additionalProperties": _inside},
                "templates": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,59}$"},
                "output": {"type": "string", "pattern": "^[^/\\\\]+$"},
                "connector": {"type": "string", "pattern": _SNAKE},
                "tool": {"type": "string", "pattern": _TOOL},
                "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        },
        "primary_metric": {"type": "string", "pattern": _SNAKE},
        # What the sandbox lets it use (ADR 0021): the hosts it reaches (`localhost` alone: its
        # own loopback servers), the Services whose URLs it calls, the data directory's folders it
        # reads (`{world}` is the world it runs for), and the hosts its install step reaches
        # besides its runtime's registry.
        "permissions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "network": _domains,
                "services": _strings,
                "folders": {
                    "type": "array",
                    "items": {"type": "string", "pattern": _FOLDER},
                },
                "install_network": _domains,
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
    ref: str | None = None
    """The tag, branch or commit it was installed at, for one installed from git."""
    commit: str | None = None
    """The commit that ref resolved to, for one installed from git."""
    subfolder: str | None = None
    """The folder of the repository the package is in, for one not at its root."""


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

    session = manifest.get("build_session")
    if ("build_session" in manifest.get("capabilities", [])) != bool(session):
        bad("build_session", "is the [build_session] of the build_session capability, both or none")
    servers = ("command" in manifest) + ("url" in manifest)
    # A Build Session calling another package's tool needs no server of its own.
    if servers == 2 or (servers == 0 and not (session and "connector" in session)):
        bad("command", "give the command that starts its MCP server, or its url, not both")
    if (manifest["kind"] == "notify") == bool(manifest["stage_types"]):
        bad("stage_types", "a notify connector runs in no stage; every other runs in one")
    if "listen" in manifest and "remote_approvals" not in manifest.get("capabilities", []):
        bad("listen", "is the long-lived process of a remote_approvals connector")
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
        if kind == "oauth":
            for suffix in ("_client_id", "_client_secret"):  # its app's two shared Credentials
                if field["key"] + suffix in keys:
                    bad(f"{at}.key", f"{field['key']}{suffix} is declared twice")
                keys[field["key"] + suffix] = field
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
    for i, folder in enumerate(manifest.get("permissions", {}).get("folders", [])):
        if folder.split("/")[0] == "sandbox":  # other processes' scratch folders
            bad(f"permissions.folders[{i}]", "sandbox is the connector sandbox's own")
    folders = [f["folder"] for f in manifest.get("owned_folders", [])]
    for i, folder in enumerate(folders):
        if folder in folders[:i]:
            bad(f"owned_folders[{i}].folder", f"{folder} is declared twice")
    if "primary_metric" in manifest and "outcomes" not in manifest.get("capabilities", []):
        bad("primary_metric", "names an outcome, so the package offers the outcomes capability")
    if "outcomes" in manifest.get("capabilities", []):
        if manifest["kind"] != "publish":
            bad("capabilities", "outcomes are a publish connector's, so its kind is publish")
        if "primary_metric" not in manifest:
            bad("primary_metric", "is required with outcomes: the one the scorecard ranks by")
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
    check_manifest(manifest)
    session = manifest.get("build_session", {})
    files = {k: session[k] for k in ("system", "brief", "starter") if k in session}
    files |= {f"notes.{k}": v for k, v in session.get("notes", {}).items()}
    for field, path in files.items():
        if not (folder / path).exists():
            raise ManifestError(f"build_session.{field}: {path} is not in the package")
    return manifest


def _changes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
    }


def permissions(manifest: dict[str, Any]) -> list[str]:
    """Every permission the Manifest declares, as `network: api.example.org`, which the operator
    approves before anything of the package runs (ADR 0021): the hosts it reaches (`network`),
    the hosts its install step reaches with its runtime's registries (`install_network`), the
    data directory's folders it reads (`folders`), the Services it calls (`services`), the world
    folders it owns (`owned_folders`), the Credentials it is given (`credentials`, by the
    variable it reads), the hosts Swarmify signs in to for it (`sign_in`) and the core's
    Capabilities it uses (`capabilities`). `{}` declares none."""
    granted = manifest.get("permissions", {})
    out = [f"network: {d}" for d in granted.get("network", [])]
    if manifest.get("install"):
        registries = REGISTRIES.get(manifest.get("runtime", ""), [])
        out += [f"install_network: {d}" for d in [*registries, *granted.get("install_network", [])]]
    out += [f"folders: {f}" for f in granted.get("folders", [])]
    out += [f"services: {s}" for s in granted.get("services", [])]
    out += [f"owned_folders: {f['folder']}" for f in manifest.get("owned_folders", [])]
    out += [
        f"credentials: {setup.env_name(f)}"
        for f in setup.fields(manifest)
        if setup.secret(f) and "client_of" not in f
    ]
    for f in manifest.get("config", []):
        if f["type"] == "oauth":
            out += [f"sign_in: {domain(f['oauth'][u])}" for u in ("authorize_url", "token_url")]
    out += [f"capabilities: {c}" for c in manifest.get("capabilities", [])]
    return list(dict.fromkeys(out))


def _escaping(folder: Path) -> str | None:
    """The first symlink in the package folder that points outside it, which a copy would
    follow (a link to a key file); None when there is none. What the copy skips is not looked
    at (a `.venv` links its Python)."""
    root = folder.resolve()
    for at, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIPPED]
        for name in [*dirs, *files]:
            path = Path(at) / name
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                return str(path.relative_to(folder))
    return None


def _git(*args: str) -> str:
    """Run git on a package's clone with none of the operator's git config (no credential
    helper, no hooks, no filters an attribute could run), no prompt, an empty HOME (no
    `.netrc`) and only https and local paths. Raises InstallError saying what git said."""
    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "LANG": "C"}
        env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "https:file"}
        quiet = ["-c", "credential.helper=", "-c", f"core.hooksPath={os.devnull}"]
        try:
            done = subprocess.run(
                ["git", *quiet, *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError(f"git did not run: {exc}") from exc
    if done.returncode != 0:
        raise InstallError(f"git {args[0]} failed: {done.stderr.strip()[-600:]}")
    return done.stdout.strip()


def manifest_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changes in a switch from one version's Manifest to another's (issue 223): its
    params by property, its Setup fields by key, the fields newly required with no default
    (added, or optional before), which need a value before the switch, and its permissions
    as `network: api.example.com`."""

    def props(m: dict[str, Any]) -> dict[str, Any]:
        return (m.get("params") or {}).get("properties") or {}

    def fields(m: dict[str, Any]) -> dict[str, Any]:
        return {f["key"]: f for f in m.get("config", [])}

    def grants(m: dict[str, Any]) -> set[str]:
        return set(permissions(m)) if m else set()

    before, after = fields(old), fields(new)

    def was_required(f: dict[str, Any]) -> bool:
        was = before.get(f["key"]) or before.get(f.get("renamed_from", ""), {})
        return bool(was.get("required"))

    return {
        "params": _changes(props(old), props(new)),
        "setup": _changes(before, after),
        "required": [
            k
            for k, f in after.items()
            if f.get("required") and "default" not in f and not was_required(f)
        ],
        "permissions": {
            "added": sorted(grants(new) - grants(old)),
            "removed": sorted(grants(old) - grants(new)),
        },
    }


def _toml(value: Any) -> str:
    """`value` as a TOML value, tables inline. Raises ManifestError for a null, which TOML lacks."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a TOML basic string
    if isinstance(value, list):
        return f"[{', '.join(_toml(v) for v in value)}]"
    if isinstance(value, dict):
        return f"{{{', '.join(f'{_toml(str(k))} = {_toml(v)}' for k, v in value.items())}}}"
    raise ManifestError(f"manifest: TOML has no {value!r}")


def write_manifest(manifest: dict[str, Any]) -> str:
    """The Manifest as the text of a `swarmify-connector.toml`."""
    return "".join(f"{k} = {_toml(v)}\n" for k, v in manifest.items())


def _install_env() -> dict[str, str]:
    """What the install step sees of the app's environment: enough to find uv, npm and Python,
    and none of the app's own variables (no keys, no VIRTUAL_ENV pointing at Swarmify's venv).
    Its HOME and TMPDIR are its own scratch folder (sandbox.py)."""
    keep = ("PATH", "LANG", "UV_PYTHON", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE")
    keep += ("UV_LINK_MODE", "UV_COMPILE_BYTECODE")
    return {k: os.environ[k] for k in keep if k in os.environ}


@functools.cache
def _caches() -> dict[str, Path]:
    """uv's and npm's own caches, by the variable that points an install step at them: where
    `UV_CACHE_DIR` and `npm_config_cache` say, as in Docker, where both are in the volume the
    sandbox service shares."""
    try:
        uv = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, timeout=30)
        found = {"UV_CACHE_DIR": Path(uv.stdout.strip())} if uv.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired):
        found = {}
    npm = os.environ.get("npm_config_cache") or Path.home() / ".npm"
    return {**found, "npm_config_cache": Path(npm)}


class Packages:
    """Connector Packages installed for the whole install under `root` (`<data dir>/packages`),
    from a folder of this repository (`repo`), as `connectors/<name>`, or a local path."""

    def __init__(self, root: Path, repo: Path, sandbox: Sandbox | None = None) -> None:
        self.root = root
        self.repo = repo
        self.sandbox = sandbox or Sandbox(root.parent)

    def source(self, name: str, version: str) -> str:
        """Where an installed version came from: a folder, or `local` for one Swarmify wrote."""
        record = self.root / name / version / RECORD
        return str(json.loads(record.read_text())["source"]) if record.is_file() else ""

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

    def install_local(self, manifest: dict[str, Any]) -> Package:
        """Install a Manifest Swarmify wrote as a local package. An installed local version
        with the same Manifest is reused; else it installs as the next free `0.0.N`. Raises
        ManifestError for a bad Manifest."""
        name = manifest["name"]
        mine = [p for p in self.list() if p.name == name and p.source == LOCAL]
        for p in mine:
            if p.state == "installed" and self.manifest(name, p.version) == {
                **manifest,
                "version": p.version,
            }:
                return p
        n = len(mine) + 1
        while (self.root / name / f"0.0.{n}").exists():
            n += 1
        with tempfile.TemporaryDirectory() as folder:
            text = write_manifest({**manifest, "version": f"0.0.{n}"})
            (Path(folder) / MANIFEST).write_text(text)
            return self.install(folder, LOCAL)

    @contextmanager
    def _fetched(
        self, source: str, ref: str | None, subfolder: str | None = None
    ) -> Iterator[tuple[Path, str | None]]:
        """The package folder at `source` (its `subfolder` when given, as `connectors/speech`)
        and, for a git source, the commit checked out. A git source is an https URL, or a local
        path given a `ref`; it is cloned into a temporary folder, removed after, running none of
        its code. Raises InstallError for any other URL, one carrying credentials, a bad ref or
        subfolder, a git failure or a symlink out of the package."""
        scheme = urlsplit(source).scheme
        if "://" in source and scheme != "https":
            raise InstallError(f"{scheme}:// is refused: install from an https:// URL or a path")
        if scheme == "https" and (urlsplit(source).username or urlsplit(source).password):
            raise InstallError("the URL carries credentials: only public repositories install")
        if ref is not None and not _REF.fullmatch(ref):
            raise InstallError(f"{ref} is not a tag, branch or commit")
        if subfolder is not None and not _SUBFOLDER.fullmatch(subfolder):
            raise InstallError(f"{subfolder} is not a folder inside the repository")

        def inside(root: Path) -> Path:
            folder = (root / subfolder) if subfolder else root
            if not folder.resolve().is_relative_to(root.resolve()) or not folder.is_dir():
                raise InstallError(f"{source} has no folder {subfolder}")
            return folder

        if scheme != "https" and ref is None:
            folder = inside(self._source(source))
            if link := _escaping(folder):
                raise InstallError(f"{link} is a symlink out of the package folder")
            yield folder, None
            return
        url = source if scheme == "https" else str(self._source(source))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "clone"
            _git("clone", "--quiet", "--no-checkout", "--template=", "--", url, str(folder))
            commit = None
            for rev in [ref, f"origin/{ref}"] if ref else ["HEAD"]:
                try:
                    at = ["-C", str(folder), "rev-parse", "--verify", "--end-of-options"]
                    commit = _git(*at, f"{rev}^{{commit}}")
                    break
                except InstallError:
                    continue
            if commit is None:
                raise InstallError(f"{source} has no tag, branch or commit {ref}")
            _git("-C", str(folder), "checkout", "--quiet", "--detach", commit)
            folder = inside(folder)
            if link := _escaping(folder):
                raise InstallError(f"{link} is a symlink out of the package folder")
            yield folder, commit

    def _newest(self, name: str) -> str | None:
        """The highest version of `name` installed and valid, which an install upgrades from."""
        versions = [p.version for p in self.list() if p.name == name and p.state == "installed"]

        def key(v: str) -> tuple[int, ...]:
            return tuple(int(n) for n in v.split("-")[0].split("+")[0].split("."))

        return max(versions, key=key, default=None)

    def review(
        self, source: str, ref: str | None = None, subfolder: str | None = None
    ) -> dict[str, Any]:
        """What installing `source` at `ref` (its `subfolder`, when given) would grant, read
        without running any of it: its name, version, label and about, the commit a git source
        resolved to, every permission it declares, and for an upgrade what they add and remove
        since the newest installed version (`upgrade`, None for a first install). Raises
        ManifestError or InstallError."""
        with self._fetched(source, ref, subfolder) as (folder, commit):
            m = read_manifest(folder)
        if (self.root / m["name"] / m["version"] / RECORD).exists():
            raise InstallError(f"{m['name']} {m['version']} is already installed")
        newest = self._newest(m["name"])
        upgrade = None
        if newest:
            before, after = set(permissions(self.manifest(m["name"], newest))), permissions(m)
            upgrade = {
                "from": newest,
                "added": [p for p in after if p not in before],
                "removed": sorted(before - set(after)),
            }
        about = {k: m[k] for k in ("name", "version", "label", "about")}
        return {**about, "commit": commit, "permissions": permissions(m), "upgrade": upgrade}

    def install(
        self,
        source: str,
        record_as: str | None = None,
        *,
        ref: str | None = None,
        commit: str | None = None,
        approved: Collection[str] | None = None,
        subfolder: str | None = None,
    ) -> Package:
        """Copy the package at `source` (cloned at `commit`, the one reviewed, else at `ref`;
        its `subfolder`, when given) to `<root>/<name>/<version>/` and run its install step
        there. `approved`, the permissions the operator approved, must hold every one it
        declares, or nothing of it runs; None for this repository's own packages and local
        ones, which Swarmify trusts. Raises
        ManifestError for a bad Manifest, InstallError when a permission is not approved, the
        version is installed already or the step fails; either way nothing new is left
        behind."""
        with self._fetched(source, commit or ref, subfolder) as (folder, resolved):
            record = {"source": record_as or source}
            if subfolder:
                record["subfolder"] = subfolder
            return self._install(folder, record, ref or commit, resolved, approved)

    def _install(
        self,
        folder: Path,
        source: dict[str, str],
        ref: str | None,
        commit: str | None,
        approved: Collection[str] | None,
    ) -> Package:
        manifest = read_manifest(folder)
        if approved is not None:
            unapproved = [p for p in permissions(manifest) if p not in approved]
            if unapproved:
                raise InstallError(f"approve these permissions first: {', '.join(unapproved)}")
        target = self.root / manifest["name"] / manifest["version"]
        if (target / RECORD).exists():
            raise InstallError(f"{manifest['name']} {manifest['version']} is already installed")
        # ponytail: no lock; two installs of one version at once race. Add one if the UI can.
        shutil.rmtree(target, ignore_errors=True)  # what a crash mid-install left
        try:
            shutil.copytree(folder, target, ignore=COPY_SKIPS)
            if manifest.get("install"):
                self._run_install(manifest, target, folder.is_relative_to(self.repo.resolve()))
            record: dict[str, Any] = {
                **source,
                "installed_at": datetime.now(UTC).isoformat(),
            }
            if commit:
                record |= {"ref": ref or "HEAD", "commit": commit}
            (target / f"{RECORD}.tmp").write_text(json.dumps(record))
            (target / f"{RECORD}.tmp").replace(target / RECORD)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise
        return self._package(target / RECORD)

    def _run_install(self, manifest: dict[str, Any], target: Path, ours: bool) -> None:
        """The install step, in the sandbox (ADR 0021): it writes only its version folder and
        reaches only its runtime's registries and the hosts `permissions.install_network`
        names. A package of this repository (`ours`) also uses uv's and npm's own caches; any
        other starts with empty ones of its own, so it cannot plant a poisoned download for the
        next install. What the sandbox blocked is logged, and said in the error when it fails."""
        name, step = manifest["name"], manifest["install"]
        extra = manifest.get("permissions", {}).get("install_network", [])
        grants = Grants(writes=[target], domains=[*REGISTRIES[manifest["runtime"]], *extra])
        env = _install_env()
        if ours:  # this repository's code may share the caches, which it cannot poison
            for var, cache in _caches().items():
                cache.mkdir(parents=True, exist_ok=True)
                env[var] = str(cache)
                grants.writes.append(cache)
        try:
            confined = self.sandbox.wrap(step, target, grants, env, path=env.get("PATH"))
            process = subprocess.Popen(
                confined.argv,
                cwd=target,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise InstallError(f"{name}'s install step did not run: {exc}") from exc
        try:
            out, err = process.communicate(timeout=INSTALL_SECONDS)
        except subprocess.TimeoutExpired as exc:
            process.terminate()  # the launcher stops the sandbox's processes in turn
            process.communicate()
            raise InstallError(f"{name}'s install step took over {INSTALL_SECONDS} s") from exc
        blocked = confined.blocked()
        if blocked:
            log.warning(
                "%s's install step was blocked by the sandbox: %s", name, "; ".join(blocked)
            )
        if process.returncode != 0:
            tail = (err or out).strip()[-600:]
            said = f" The sandbox blocked: {'; '.join(blocked)}." if blocked else ""
            raise InstallError(
                f"{name}'s install step `{' '.join(step)}` failed ({process.returncode}):"
                f" {tail}{said}"
            )

    def uninstall(self, name: str, version: str) -> None:
        """Remove an installed version's folder. Raises InstallError when it is not installed.
        Whether anything still uses it is the caller's to check first."""
        installed = re.fullmatch(_SNAKE, name) and re.fullmatch(_VERSION, version)
        folder = self.root / name / version
        if not installed or not (folder / RECORD).is_file():
            raise InstallError(f"{name} {version} is not installed")
        (folder / RECORD).unlink()  # first, so a half-removed folder never counts as installed
        shutil.rmtree(folder)
        if not any(folder.parent.iterdir()):
            folder.parent.rmdir()
