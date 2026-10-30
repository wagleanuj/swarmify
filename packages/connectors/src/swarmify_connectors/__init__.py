"""Connectors context: the only way the swarm touches anything outside the box.

A Connector is an MCP server plus its credentials plus the stage types allowed to use it. The
built-ins live in code; every other is an installed Connector Package, giving a world a Connector
named as the package only once the world enables it at a pinned version (ADR 0023). The "add an
MCP server" form writes a minimal Manifest and installs it as a local package (issue 211). This
repository's own packages are the defaults every world is given once (`install_defaults`).
A connector stage calls one Tool through the ConnectorTransport port with the stage id as its
Idempotency Key, records the raw call and response, and polls a long job until it ends,
appending a `connector_call.updated` event per change so the timeline shows progress.

A long job is a tool answer carrying `job_id` and `poll`, the tool to poll with
`{"job_id": ...}`; each poll answers `status` (running, done or failed) and optionally
`progress` (0 to 1), `message` and `error`.
"""

import hmac
import logging
import os
import re
import secrets
import shlex
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from swarmify_connectors import builtin_publish, oauth, publish, remotion, setup
from swarmify_connectors.credentials import Credentials
from swarmify_connectors.fake import FakeTransport
from swarmify_connectors.mcp_transport import McpTransport
from swarmify_connectors.model import (
    KINDS,
    STAGE_TYPES,
    Call,
    Connector,
    ConnectorError,
    HealthCheck,
    KeyCheck,
    Tool,
)
from swarmify_connectors.packages import (
    MANIFEST,
    InstallError,
    ManifestError,
    Package,
    Packages,
    manifest_diff,
    read_manifest,
)
from swarmify_connectors.ports import ConnectorTransport, Storage
from swarmify_connectors.setup import SetupStore
from swarmify_connectors.sqlite import MIGRATIONS, SqliteStorage
from swarmify_kernel import AGENT_TOOLS, new_id

__all__ = [
    "BUILTINS",
    "KINDS",
    "MIGRATIONS",
    "STAGE_TYPES",
    "Call",
    "Connector",
    "ConnectorError",
    "ConnectorTransport",
    "Connectors",
    "Credentials",
    "FakeTransport",
    "HealthCheck",
    "InstallError",
    "KeyCheck",
    "ManifestError",
    "McpTransport",
    "Package",
    "Packages",
    "SetupStore",
    "SqliteStorage",
    "Storage",
    "Tool",
    "from_manifest",
    "read_manifest",
]

log = logging.getLogger(__name__)
DEFAULTS = "connectors"
"""The folder of this repository whose packages are the defaults every world is given."""
NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
TYPE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
"""A declared type's name, as a swarm stage's `output_type` names it."""


def from_manifest(m: dict[str, Any]) -> Connector:
    """The built-in Connector a Manifest declares; it keeps the Manifest for the planner."""
    return Connector(
        f"builtin_{m['name']}",
        None,
        m["name"],
        m["kind"],
        m.get("command", []),
        credentials=m.get("credentials", []),
        stage_types=m["stage_types"],
        builtin=True,
        about=m["about"],
        health=m.get("health"),
        types=m.get("types", {}),
        input_types=m.get("input_types", []),
        world_types=m.get("world_types"),
        manifest=m,
    )


_SCRIPT_IN = {"description": "the script", "media_type": "text/markdown"}
_VIDEO_IN = {"description": "the video", "media_type": "video/mp4"}
_PUBLISH_PARAMS: dict[str, Any] = {
    "title": {
        "type": "string",
        "description": "required unless metadata names a file an earlier stage produced whose"
        " frontmatter has title and description",
    },
    "metadata": {
        "type": "string",
        "description": "a file an earlier stage produced; its frontmatter's title, description,"
        " hashtags and tags win over these params",
    },
    "description": {"type": "string"},
    "visibility": {"enum": ["public", "unlisted", "private"], "default": "private"},
    "schedule": {
        "type": "string",
        "description": "ISO 8601 with a timezone, or 09:00, or +1d 09:00; needs public",
    },
    "made_for_kids": {"type": "boolean", "default": False},
}

# Built-ins declared by a manifest in the shape of a V2 connector package's (ADR 0019): the core
# and the planner read only these fields, so moving one into a package moves its module and
# this entry.
MANIFESTS: dict[str, dict[str, Any]] = {
    m["name"]: m
    for m in (
        {
            "name": "remotion_render",
            "label": "Remotion render",
            "kind": "stage",
            "stage_types": ["connector"],
            "about": "Renders a script with the Remotion CLI. Needs Node and a Remotion project.",
            "command": [sys.executable, "-m", "swarmify_connectors.remotion"],
            "params": {
                "type": "object",
                "required": ["project", "composition"],
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "a template's name under the data directory's remotion"
                        " folder, or a folder path",
                    },
                    "composition": {"type": "string", "description": "its composition ID"},
                    "entry": {"type": "string", "default": "src/index.ts"},
                    "codec": {"type": "string", "default": "h264"},
                    "frames": {"type": "string", "description": "a range, as 0-29"},
                    "props": {"type": "object", "description": "the composition's input props"},
                },
            },
            "planner_notes": "Use it only when the operator has a finished template; else"
            " remotion_build makes the video.",
            "inputs": [_SCRIPT_IN],
            "outputs": [{"description": "the video", "media_type": "video/mp4"}],
        },
        {
            "name": "remotion_build",
            "label": "Remotion build",
            "kind": "stage",
            "stage_types": ["connector"],
            "about": "Writes a Remotion project from the script in a sandboxed coding session,"
            " then renders it. Needs Node.",
            "params": {
                "type": "object",
                "properties": {
                    "base": {
                        "type": "string",
                        "description": "a template's name under the data directory's remotion"
                        " folder to start from, else a minimal starter",
                    },
                    "composition": {"type": "string", "default": "Main"},
                    "brief": {
                        "type": "string",
                        "description": "free text for the builder: style, length, pacing",
                    },
                    "model": {"type": "string"},
                    "max_turns": {"type": "integer", "default": 80},
                    "minutes": {
                        "type": "integer",
                        "default": 20,
                        "description": "the session's time limit",
                    },
                },
            },
            "planner_notes": "This is how a workflow makes a video. One sandboxed coding session"
            " writes a Remotion project for the script, reading the run's gate decisions, then"
            " swarmify renders it. It costs a coding session from the run's budget. Give it the"
            " script as input, or inputs: the script first, then files the builder should also"
            " read (as speech's timings.json).",
            "inputs": [_SCRIPT_IN],
            "outputs": [
                {"description": "the video, video.mp4 by default", "media_type": "video/mp4"}
            ],
            "capabilities": ["build_session"],
        },
        {
            "name": "youtube",
            "label": "YouTube",
            "kind": "publish",
            "stage_types": ["connector"],
            "about": "Uploads to YouTube through the Data API; a schedule keeps it private until"
            " then.",
            "command": [sys.executable, "-m", "swarmify_connectors.youtube"],
            "credentials": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
            "params": {"type": "object", "properties": _PUBLISH_PARAMS},
            "inputs": [_VIDEO_IN],
        },
    )
}

BUILTINS = [from_manifest(m) for m in MANIFESTS.values()]


# The cheap read-only tool each built-in publish connector proves its keys with.
KEY_CHECKS = {"youtube": "channel"}

KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")
"""A key name the add form takes; lower-cased, it is its Setup field's key."""
_RUNTIMES = {
    "npx": "npm",
    "node": "npm",
    "npm": "npm",
    "docker": "container",
    "podman": "container",
}


def _local_manifest(c: Connector) -> dict[str, Any]:
    """The minimal Manifest of a connector the add form drafted: each key name a required,
    shared Credential reaching the process as that name."""
    server = c.url or shlex.join(c.command)
    m: dict[str, Any] = {
        "name": c.name,
        "version": "0.0.0",  # install_local picks the real one
        "label": c.name,
        "about": f"An MCP server added on the Connectors page: {server}",
        "kind": c.kind,
        "stage_types": c.stage_types,
        "runtime": "uv" if c.url else _RUNTIMES.get(Path(c.command[0]).name, "uv"),
        **({"url": c.url} if c.url else {"command": c.command}),
        "config": [
            {"key": n.lower(), "label": n, "type": "string", "secret": True, "required": True}
            | {"scope": "shared", "env": n}
            for n in c.credentials
        ],
    }
    if c.types:
        m["types"] = c.types
    if c.input_types:
        m["input_types"] = [t or "" for t in c.input_types]  # TOML has no null: "" is untyped
    if c.world_types:
        m["world_types"] = c.world_types
    return m


def _account(connector: str, answer: dict[str, Any]) -> str:
    if connector == "youtube" and answer.get("channel"):
        return f"Posts to the YouTube channel {answer['channel']}."
    return "The keys work."


def _is_schema(schema: dict[str, Any]) -> bool:
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError:
        return False
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _progress(value: Any, before: float | None) -> float | None:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return before


def _manifest_problems(
    manifest: dict[str, Any], config: dict[str, Any], params: dict[str, Any]
) -> list[str]:
    """A stage's input, output and params against what its connector's manifest declares."""
    name, out = manifest["name"], []
    if manifest.get("inputs") and not (config.get("input") or config.get("inputs")):
        out.append(f"{name} needs an input, {manifest['inputs'][0]['description']}")
    if manifest.get("outputs") and not config.get("output"):
        out.append(f"{name} needs an output, {manifest['outputs'][0]['description']}")
    schema = manifest.get("params") or {}
    props = schema.get("properties") or {}
    for k in schema.get("required") or []:
        if k not in params:
            said = props.get(k, {}).get("description")
            out.append(f"{name} needs params.{k}" + (f", {said}" if said else ""))
    if schema.get("additionalProperties") is False:
        allowed = {*props, "tool"}
        out += [f"{name} does not take params.{k}" for k in params if k not in allowed]
    return out


class Connectors:
    def __init__(
        self,
        storage: Storage,
        transport: ConnectorTransport,
        credentials: Credentials,
        poll_seconds: float = 2.0,
        packages: Packages | None = None,
    ) -> None:
        self.storage = storage
        self.transport = transport
        self.credentials = credentials
        self.poll_seconds = poll_seconds
        self.data_dir = credentials.path.parent  # credentials live in `<data dir>/.env`
        self.packages = packages or Packages(self.data_dir / "packages", self.data_dir)
        self.setups = SetupStore(self.data_dir / "setup.json")
        self._sign_ins: dict[str, dict[str, Any]] = {}
        """Sign-ins under way by their `state`, each bound to the browser session it began in."""
        self._tokens = threading.Lock()
        """One refresh at a time, so a rotated refresh token is never spent twice."""
        self.defaults: dict[str, str] = {}
        """Default package name to the version a world is given (`install_defaults`)."""

    # Registry

    def list_all(self, world_id: str, pins: Mapping[str, str] | None = None) -> list[Connector]:
        """Built-ins first, then the packages the world enables, by name. A run passes `pins`,
        the package versions its workflow version recorded (issue 223), which win over the
        world's current pins."""
        enabled = self.storage.enabled_packages(world_id)
        chosen = {n: (pins or {}).get(n, v) for n, v in enabled.items()}
        packaged = [self.package_connector(world_id, n, v) for n, v in chosen.items()]
        return [*BUILTINS, *(c for c in packaged if c)]

    # Connector Packages a world enables (ADR 0023)

    def package_connector(self, world_id: str, name: str, version: str) -> Connector | None:
        """The Connector an installed package version gives a world, read from its Manifest;
        None, logged, when that version is gone or its Manifest no longer passes."""
        try:
            m = self.packages.manifest(name, version)
        except (InstallError, ManifestError) as exc:
            log.warning("%s enables %s %s, which is unusable: %s", world_id, name, version, exc)
            return None
        return Connector(
            f"package_{name}",
            world_id,
            name,
            m["kind"],
            m.get("command", []),
            m.get("url"),
            [
                setup.env_name(f)
                for f in setup.fields(m)
                if setup.secret(f) and "client_of" not in f
            ],
            stage_types=m["stage_types"],
            about=m["about"],
            types=m.get("types", {}),
            input_types=[t or None for t in m.get("input_types", [])],
            health=m.get("health"),
            world_types=m.get("world_types"),
            version=version,
            folder=str(self.packages.root / name / version),
            manifest=m,
        )

    def enabled(self, world_id: str) -> dict[str, str]:
        """Package name to the version the world pins, for each package it enables."""
        return self.storage.enabled_packages(world_id)

    def enable(self, world_id: str, name: str, version: str) -> None:
        """Enable an installed package in the world at `version`, or move its pin there.
        Raises ValueError when that version is not installed or valid, or the name is taken."""
        if any(c.name == name for c in BUILTINS):
            raise ValueError(f"'{name}' is a built-in connector")
        if name in AGENT_TOOLS:
            raise ValueError(f"'{name}' is an Agent Tool the core gives swarm stages")
        try:
            self.packages.manifest(name, version)
        except (InstallError, ManifestError) as exc:
            raise ValueError(str(exc)) from exc
        needs = self.switch(world_id, name, version)["needs"]
        if needs:
            raise ValueError(f"{name} {version} needs a value for {', '.join(needs)} first")
        self.storage.enable_package(world_id, name, version, _now())

    def switch(self, world_id: str, name: str, version: str) -> dict[str, Any]:
        """What moving the world's pin of `name` to `version` changes in its Manifest (params,
        Setup fields, permissions), from the pinned version (none when not enabled), and
        `needs`: the labels of newly required Setup fields with no value in this world, which
        hold a switch back (a first enable sets them up after). Raises ValueError when a
        version is not installed or valid."""
        pinned = self.storage.enabled_packages(world_id).get(name)
        try:
            new = self.packages.manifest(name, version)
            old = self.packages.manifest(name, pinned) if pinned else {}
        except (InstallError, ManifestError) as exc:
            raise ValueError(str(exc)) from exc
        diff = manifest_diff(old, new)
        labels = {f["key"]: f["label"] for f in setup.fields(new)}
        unset = setup.missing(new, setup.resolve(new, *self.setups.values(name, world_id)))
        # a first enable never waits: its Setup form is filled in once it is enabled
        needs = [labels[k] for k in diff["required"] if pinned and k in unset]
        return {"from": pinned, "to": version, **diff, "needs": needs}

    def pinned_by(self, name: str, version: str) -> list[str]:
        """The worlds that pin `name` at `version`."""
        return self.storage.package_pinned_by(name, version)

    def disable(self, world_id: str, name: str) -> bool:
        """Stop the world using the package; other worlds keep theirs. False if not enabled."""
        return self.storage.disable_package(world_id, name)

    # Default packages (ADR 0023): this repository's own, given to every world

    def install_defaults(self) -> dict[str, str]:
        """Install each package of this repository's `connectors/` folder whose version is not
        installed yet, and keep them as the defaults worlds are given. The first install of a
        package carries what V1 kept for it into its shared Setup: a Setting from the app's env
        and a Credential from the credentials file, each found by its field's `env` name. One
        that does not install is logged and left out; the others still are."""
        for manifest in sorted((self.packages.repo / DEFAULTS).glob(f"*/{MANIFEST}")):
            source = f"{DEFAULTS}/{manifest.parent.name}"
            try:
                m = read_manifest(manifest.parent)
                first = not (self.packages.root / m["name"]).is_dir()
                try:
                    self.packages.manifest(m["name"], m["version"])
                except InstallError:
                    self.packages.install(source)
            except (ManifestError, InstallError) as exc:
                log.error("the default package %s did not install: %s", source, exc)
                continue
            if first:
                self._carry(m)
            self.defaults[m["name"]] = m["version"]
        return self.defaults

    def _carry(self, m: dict[str, Any]) -> None:
        try:
            shared, _ = self.setups.values(m["name"], "")
            saved = self.credentials.read()
        except PermissionError as exc:
            log.error("%s's Setup was not carried over from V1: %s", m["name"], exc)
            return
        carried = dict(shared)
        for f in setup.fields(m):
            env = setup.env_name(f)
            raw = saved.get(env) if f.get("secret") else os.environ.get(env)
            # an OAuth sign-in holds a token set, not a value from V1: its package moves it
            if not raw or f["key"] in carried or setup.scope(f) == "world" or f["type"] == "oauth":
                continue
            try:
                carried[f["key"]] = setup.coerce(f, raw)
            except ValueError as exc:
                log.warning("%s's %s was not carried over: %s", m["name"], env, exc)
        for key in carried.keys() - shared.keys():
            self.setups.put_value(m["name"], None, key, carried[key])

    def offer_defaults(self, world_id: str) -> None:
        """Enable in the world each default package it has not been given yet, at the default
        version; given once, the world's own choice stands, disabling it included."""
        given = self.storage.offered_defaults(world_id)
        for name, version in self.defaults.items():
            if name in given:
                continue
            if name not in self.storage.enabled_packages(world_id):
                try:
                    self.enable(world_id, name, version)
                except ValueError as exc:
                    log.warning("%s was not given the default package %s: %s", world_id, name, exc)
            self.storage.offer_default(world_id, name, _now())

    # Setup of a package a world enables (ADR 0024)

    def setup_values(
        self, world_id: str, name: str, version: str | None = None
    ) -> tuple[Connector, setup.Values, setup.Values]:
        """The enabled package's Connector, at the pinned version or the installed `version` a
        switch moves to (whose new fields are filled in before it), its shared Setup values and
        the world's own. Raises ValueError when the world does not enable it."""
        connector = self.find(world_id, name, {name: version} if version else None)
        if connector is None or connector.version is None:
            raise ValueError(f"this world does not enable {name}")
        return connector, *self.setups.values(name, world_id)

    def save_setup(
        self,
        world_id: str,
        name: str,
        shared: dict[str, Any],
        world: dict[str, Any],
        version: str | None = None,
    ) -> KeyCheck | None:
        """Set the package's shared values and the world's own, for the fields its Manifest
        declares. An empty value clears a Setting; an empty Credential keeps the saved one and
        None clears it. Every required field must then resolve for this world. When the package
        declares a `[setup] test` tool, it runs with the candidate values first: the values are
        saved only when it answers `ok`, and its `message` is returned either way. Raises
        ValueError naming the first bad field."""
        connector, now_shared, now_world = self.setup_values(world_id, name, version)
        declared = {f["key"]: f for f in setup.fields(connector.manifest)}
        for values, new, where in ((now_shared, shared, "shared"), (now_world, world, "world")):
            for f in declared.values():  # carry a renamed field's value to its key
                old = f.get("renamed_from")
                if old in values and f["key"] not in values:
                    values[f["key"]] = values.pop(old)
            for key, raw in new.items():
                field = declared.get(key)
                if field is None:
                    raise ValueError(f"{name} has no Setup field {key}")
                if where == "shared" and setup.scope(field) == "world":
                    raise ValueError(f"{field['label']}: is set per world, not shared")
                if setup.secret(field) and raw == "":
                    continue
                value = setup.coerce(field, raw)
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
        resolved = setup.resolve(connector.manifest, now_shared, now_world)
        gaps = setup.missing(connector.manifest, resolved)
        if gaps := [k for k in gaps if declared[k]["type"] != "oauth"]:  # signed in apart
            raise ValueError(f"{declared[gaps[0]]['label']}: is required")
        check = None
        if test := connector.manifest.get("setup", {}).get("test"):
            env = setup.environment(
                connector.manifest, self._signed_in(connector, now_shared, now_world)
            )
            try:
                answer = self._tool(connector, test, {}, env)
                ok = answer.get("ok") is True
                said = str(answer.get("message") or "")
                check = KeyCheck(ok, said or ("The setup works." if ok else "The test failed."))
            except ConnectorError as exc:
                check = KeyCheck(False, str(exc))
            if not check.ok:
                return check
        self.setups.put(name, world_id, now_shared, now_world)
        return check

    # OAuth sign-in for an `oauth` Setup field (spec V2, OAuth)

    def start_sign_in(
        self, world_id: str, name: str, key: str, redirect_uri: str, bound_to: str
    ) -> str:
        """Where to send the browser to sign in to the package's oauth field `key`. The one-time
        state is bound to `bound_to`, a digest of the session asking, and lasts ten minutes.
        Raises ValueError when the field is unknown or its app's client ID is not saved."""
        connector, shared, _ = self.setup_values(world_id, name)
        field = self._oauth_field(connector, key)
        client_id = shared.get(f"{key}_client_id")
        if not client_id:
            raise ValueError(f"{field['label']}: save its client ID first, then sign in")
        state, verifier = secrets.token_urlsafe(32), oauth.new_verifier()
        now = time.monotonic()
        self._sign_ins = {s: p for s, p in self._sign_ins.items() if p["until"] > now}
        self._sign_ins[state] = {
            "world_id": world_id,
            "name": name,
            "key": key,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "bound_to": bound_to,
            "until": now + 600,
        }
        return oauth.authorize_url(field, client_id, redirect_uri, state, verifier)

    def sign_in_for(self, state: str, bound_to: str) -> dict[str, Any]:
        """The sign-in `state` names, used up, when it is current and began in the same session.
        Raises ValueError otherwise."""
        pending = self._sign_ins.pop(state, None)
        if (
            pending is None
            or pending["until"] < time.monotonic()
            or not hmac.compare_digest(pending["bound_to"], bound_to)
        ):
            raise ValueError(
                "This sign-in is stale or began in another browser. Start it again from the"
                " Connectors page."
            )
        return pending

    def finish_sign_in(self, pending: dict[str, Any], code: str) -> None:
        """Trade the provider's code for the token set and store it as the field's Credential:
        shared, or the world's own for a field set per world. Raises ValueError (or
        oauth.SignInNeeded) saying why, never with a token in it."""
        world_id, name, key = pending["world_id"], pending["name"], pending["key"]
        connector, shared, _ = self.setup_values(world_id, name)
        field = self._oauth_field(connector, key)
        client = (
            str(shared.get(f"{key}_client_id", "")),
            str(shared.get(f"{key}_client_secret", "")),
        )
        tokens = oauth.exchange(field, client, code, pending["verifier"], pending["redirect_uri"])
        self.setups.put_value(name, self._token_world(connector, field), key, tokens)
        log.info("%s signed in to %s for %s", world_id, name, key)

    def sign_in_needed(self, connector: Connector) -> list[str]:
        """Keys of the package's oauth fields whose token set the provider refused."""
        if connector.version is None or connector.world_id is None:
            return []
        try:
            shared, world = self.setups.values(connector.name, connector.world_id)
        except PermissionError:
            return []
        resolved = setup.resolve(connector.manifest, shared, world)
        return [
            f["key"]
            for f in setup.fields(connector.manifest)
            if setup.needs_sign_in(f, resolved.get(f["key"]))
        ]

    def _oauth_field(self, connector: Connector, key: str) -> dict[str, Any]:
        for f in connector.manifest.get("config", []):
            if f["key"] == key and f["type"] == "oauth":
                return dict(f)
        raise ValueError(f"{connector.name} has no sign-in field {key}")

    def _token_world(self, connector: Connector, field: dict[str, Any]) -> str | None:
        # ponytail: a shared oauth field signs in once for every world; no per-world override.
        return connector.world_id if setup.scope(field) == "world" else None

    def _signed_in(
        self, connector: Connector, shared: setup.Values, world: setup.Values
    ) -> setup.Values:
        """The package's resolved values with each oauth token set swapped for a current access
        token: refreshed first when it is near expiry, the refreshed set stored (and set in
        `shared` or `world`). A refused refresh marks the field for sign-in again."""
        resolved = setup.resolve(connector.manifest, shared, world)
        with self._tokens:
            for f in connector.manifest.get("config", []):
                tokens = resolved.get(f["key"])
                if f["type"] != "oauth" or not isinstance(tokens, dict):
                    continue
                where = world if setup.scope(f) == "world" else shared
                key = f["key"]
                client = (
                    str(shared.get(f"{key}_client_id", "")),
                    str(shared.get(f"{key}_client_secret", "")),
                )
                try:
                    now = oauth.fresh(f, client, tokens)
                except oauth.SignInNeeded as exc:
                    log.warning("%s needs sign-in again: %s", connector.name, exc)
                    now = {**tokens, "needs_sign_in": True}
                except ValueError as exc:
                    raise ConnectorError(str(exc)) from None
                if now != tokens:
                    where[key] = now
                    self.setups.put_value(connector.name, self._token_world(connector, f), key, now)
                resolved[key] = now if now.get("needs_sign_in") else now["access_token"]
        return resolved

    def _tool(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        """Call one tool. A package's tool failing with an `auth_failed` message marks
        its oauth fields for sign-in again (oauth.is_auth_failure)."""
        try:
            return self.transport.call(connector, tool, arguments, env)
        except ConnectorError as exc:
            if connector.version is not None and oauth.is_auth_failure(str(exc)):
                self._needs_sign_in(connector)
            raise

    def _needs_sign_in(self, connector: Connector) -> None:
        assert connector.world_id is not None
        with self._tokens:
            shared, world = self.setups.values(connector.name, connector.world_id)
            for f in connector.manifest.get("config", []):
                where = world if setup.scope(f) == "world" else shared
                tokens = where.get(f["key"])
                if f["type"] == "oauth" and isinstance(tokens, dict):
                    marked = {**tokens, "needs_sign_in": True}
                    self.setups.put_value(
                        connector.name, self._token_world(connector, f), f["key"], marked
                    )
                    log.warning(
                        "%s reported an auth failure: %s needs sign-in", connector.name, f["key"]
                    )

    def find(
        self, world_id: str, name: str, pins: Mapping[str, str] | None = None
    ) -> Connector | None:
        return next((c for c in self.list_all(world_id, pins) if c.name == name), None)

    def type_schema(
        self, world_id: str, name: str, pins: Mapping[str, str] | None = None
    ) -> dict[str, Any] | None:
        """The JSON Schema of type `name` as the world's connectors declare it for this world;
        None when none does, or two declare it differently (the workflow check reports that)."""
        found = [
            self.for_world(world_id, c)[0][name]
            for c in self.list_all(world_id, pins)
            if name in c.types
        ]
        return found[0] if found and all(f == found[0] for f in found) else None

    def for_world(self, world_id: str, connector: Connector) -> tuple[dict[str, Any], str]:
        """The connector's declared types as it answers them for this world, and its note for
        the planner (issue 140). Its `world_types` tool gets the world's id and assets folder;
        each type it declares and answers with a JSON Schema takes that schema, every other
        keeps the declared one, as do all when the tool fails. The core never reads inside a
        schema, so a world-dependent enum means nothing to it."""
        if not connector.world_types:
            return connector.types, ""
        assets = self.data_dir / "worlds" / world_id / "assets"  # the run's `assets_path`
        args = {"world": world_id, "assets_path": str(assets)}
        try:
            answer = self._tool(connector, connector.world_types, args, self._env(connector))
        except ConnectorError as exc:
            log.warning("%s could not answer its types for %s: %s", connector.name, world_id, exc)
            return connector.types, ""
        answered = answer.get("types")
        types = dict(connector.types)
        for name, schema in answered.items() if isinstance(answered, dict) else ():
            if name in types and isinstance(schema, dict) and _is_schema(schema):
                types[name] = schema
        note = answer.get("note")
        return types, note.strip() if isinstance(note, str) else ""

    def get(self, connector_id: str) -> Connector | None:
        """A built-in; a package's connector has no id outside its world."""
        return next((c for c in BUILTINS if c.id == connector_id), None)

    def draft(
        self,
        world_id: str,
        name: str,
        kind: str,
        server: str,
        credentials: list[str],
        stage_types: list[str],
        declared: dict[str, Any] | None = None,
        input_types: list[str | None] | None = None,
        world_types: str | None = None,
    ) -> Connector:
        """A validated, unsaved connector, as the add form describes it. Raises ValueError
        naming the first problem."""
        name, server = name.strip(), server.strip()
        if not NAME.match(name):
            raise ValueError("a name is lower_snake_case, as workflows call it: fal_video")
        if any(c.name == name for c in BUILTINS):
            raise ValueError(f"'{name}' is a built-in connector")
        if name in AGENT_TOOLS:
            raise ValueError(f"'{name}' is an Agent Tool the core gives swarm stages")
        if name in self.storage.enabled_packages(world_id):
            raise ValueError(f"'{name}' is a Connector Package this world enables already")
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        if not server:
            raise ValueError("give an MCP command or a URL")
        url = server if server.startswith(("http://", "https://")) else None
        try:
            command = [] if url else shlex.split(server)
        except ValueError as exc:
            raise ValueError(f"cannot read the command: {exc}") from exc
        bad = [n for n in credentials if not KEY_NAME.match(n)]
        if bad:
            raise ValueError(f"credential names are UPPER_SNAKE_CASE: {', '.join(bad)}")
        types = [t for t in STAGE_TYPES if t in stage_types]
        if not types or len(types) != len(set(stage_types)):
            raise ValueError(f"allowed stage types are some of {', '.join(STAGE_TYPES)}")
        if kind == "publish" and types != ["connector"]:
            raise ValueError("a publish connector runs only in connector stages, after a gate")
        declared, input_types = declared or {}, input_types or []
        for type_name, schema in declared.items():
            if not TYPE_NAME.match(type_name):
                raise ValueError(f"a type name is lower_snake_case, as output_type: {type_name!r}")
            try:
                validator_for(schema).check_schema(schema)
            except SchemaError as exc:
                raise ValueError(f"type {type_name} is not a JSON Schema: {exc.message}") from exc
        unknown = [t for t in input_types if t is not None and t not in declared]
        if unknown:
            raise ValueError(f"input types it does not declare: {', '.join(unknown)}")
        world_types = (world_types or "").strip() or None
        if world_types and not declared:
            raise ValueError("world_types answers declared types: declare types first")
        return Connector(
            new_id("conn"),
            world_id,
            name,
            kind,  # type: ignore[arg-type]  # checked against KINDS above
            command,
            url,
            list(dict.fromkeys(credentials)),
            types,
            created_at=_now(),
            types=declared,
            input_types=list(input_types),
            world_types=world_types,
        )

    def test(self, connector: Connector, secrets: dict[str, str]) -> list[Tool]:
        """Start a drafted server once with its keys and return its tools. Saves nothing."""
        return self.transport.list_tools(
            connector, self._env(connector, self._keys(connector, secrets))
        )

    def add(self, connector: Connector, secrets: dict[str, str]) -> Connector:
        """Install a drafted connector as a local Connector Package, enable it in its world and
        save its keys as the package's shared Setup values. Raises ManifestError, or
        PermissionError for a setup or credentials file others can read."""
        assert connector.world_id is not None
        keys = self._keys(connector, secrets)  # surfaces a bad file mode before anything is saved
        shared, world = self.setups.values(connector.name, connector.world_id)
        version = self.packages.install_local(_local_manifest(connector)).version
        self.storage.enable_package(connector.world_id, connector.name, version, _now())
        self.setups.put(
            connector.name,
            connector.world_id,
            shared | {k.lower(): v for k, v in keys.items()},
            world,
        )
        found = self.find(connector.world_id, connector.name)
        assert found is not None
        return found

    def _keys(self, connector: Connector, secrets: dict[str, str]) -> dict[str, str]:
        """Each of a drafted connector's key names to its value: as entered, else its package's
        saved one, else the credentials file's. Names with none are left out."""
        assert connector.world_id is not None
        saved, _ = self.setups.values(connector.name, connector.world_id)
        file = self.credentials.read()
        out = {}
        for n in connector.credentials:
            if value := secrets.get(n) or saved.get(n.lower()) or file.get(n):
                out[n] = value
        return out

    def adopt_world_connectors(self) -> None:
        """The one-time move of every connector a world added before Connector Packages (issue
        211): each becomes a local package enabled for its world, its keys copied from the
        credentials file into its Setup, and its row goes, so workflows calling it by name run
        unchanged. The keys then leave the file unless a built-in uses them. A row that cannot
        move is logged and kept."""
        moved: set[str] = set()
        for c in self.storage.world_connectors():
            try:
                self.add(c, {})
            except (ValueError, InstallError) as exc:  # ManifestError is a ValueError
                log.warning("could not move %s of %s to a package: %s", c.name, c.world_id, exc)
                continue
            self.storage.delete_connector(c.id)
            moved.update(c.credentials)
        kept = {n for c in BUILTINS for n in c.credentials}
        kept |= {n for c in self.storage.world_connectors() for n in c.credentials}
        self.credentials.remove(moved - kept)

    def set_credentials(self, connector: Connector, values: dict[str, str]) -> KeyCheck | None:
        """Merge values for the connector's own credential names; others are refused. Then, for
        a built-in with a key check and all its keys saved, check them live: a failing check
        keeps the keys and says why, so a bad token shows now rather than at publish."""
        foreign = sorted(set(values) - set(connector.credentials))
        if foreign:
            raise ValueError(f"{connector.name} does not use {', '.join(foreign)}")
        filled = {k: v for k, v in values.items() if v}
        if filled:
            self.credentials.set(filled)
        tool = KEY_CHECKS.get(connector.name) if connector.builtin else None
        if tool is None or self.missing(connector):
            return None
        try:
            answer = self._tool(connector, tool, {}, self._env(connector))
        except ConnectorError as exc:
            return KeyCheck(False, str(exc))
        return KeyCheck(True, _account(connector.name, answer))

    def missing(self, connector: Connector) -> list[str]:
        """Credential names the file does not hold yet; for a package, the keys of required
        Setup fields that resolve to nothing in its world."""
        if connector.version is not None and connector.world_id is not None:
            try:
                return setup.missing(connector.manifest, self._resolved(connector))
            except PermissionError:
                return [f["key"] for f in setup.fields(connector.manifest) if f.get("required")]
        try:
            have = self.credentials.read()
        except PermissionError:
            have = {}
        return [n for n in connector.credentials if not have.get(n)]

    def delete_world(self, world_id: str) -> None:
        self.storage.delete_world(world_id)
        self.setups.delete_world(world_id)

    def secret_names(self, world_id: str) -> list[str]:
        """Every key name a coding session must not see: those in the file and those any of
        the world's connectors declares."""
        try:
            saved = set(self.credentials.read())
        except PermissionError:
            saved = set()
        return sorted(saved | {n for c in self.list_all(world_id) for n in c.credentials})

    def agent_servers(
        self, world_id: str, names: list[str], pins: Mapping[str, str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """The standard MCP server config of each named connector that has a server, with its
        credentials: what a swarm stage's agents connect to. Raises ConnectorError when one is
        missing a credential."""
        servers: dict[str, dict[str, Any]] = {}
        for name in names:
            connector = self.find(world_id, name, pins)
            if connector is None or not connector.has_server:
                continue
            env = self._env(connector)
            servers[name] = (
                {"type": "http", "url": connector.url, "headers": env}
                if connector.url
                else {
                    "type": "stdio",
                    "command": connector.command[0],
                    "args": connector.command[1:],
                    "env": env,
                }
            )
        return servers

    def _resolved(self, connector: Connector) -> setup.Values:
        assert connector.world_id is not None
        shared, world = self.setups.values(connector.name, connector.world_id)
        return setup.resolve(connector.manifest, shared, world)

    def _env(self, connector: Connector, extra: dict[str, str] | None = None) -> dict[str, str]:
        if connector.version is not None:
            # A package gets exactly its own Setup values for the world it runs for (ADR 0024).
            assert connector.world_id is not None
            # ponytail: the access token is fresh when the process starts; a swarm stage's agents
            # holding one server longer than the token lives would need the server to re-read it.
            resolved = self._signed_in(
                connector, *self.setups.values(connector.name, connector.world_id)
            )
            if gaps := setup.missing(connector.manifest, resolved):
                raise ConnectorError(
                    f"{connector.name} needs its Setup on the Connectors page: {', '.join(gaps)}"
                )
            return setup.environment(connector.manifest, resolved)
        values = {**self.credentials.read(), **{k: v for k, v in (extra or {}).items() if v}}
        missing = [n for n in connector.credentials if not values.get(n)]
        if missing:
            raise ConnectorError(
                f"{connector.name} needs {', '.join(missing)} in {self.credentials.path}"
            )
        env = {n: values[n] for n in connector.credentials}
        if connector.builtin and connector.name == "remotion_render":
            env["SWARMIFY_DATA_DIR"] = str(self.data_dir)  # where its templates live
            if os.environ.get(remotion.CONCURRENCY):
                env[remotion.CONCURRENCY] = os.environ[remotion.CONCURRENCY]
        return env

    def health(self, connector: Connector) -> list[HealthCheck]:
        """Whether each Service a package declares answers, then what the connector's `health`
        tool finds. Empty for one with neither. Raises ConnectorError when the tool cannot be
        called."""
        checks = self.services(connector) if connector.version is not None else []
        if not connector.health:
            return checks
        answer = self._tool(connector, connector.health, {}, self._env(connector))
        return checks + [
            HealthCheck(str(c.get("name", "")), bool(c.get("ok")), str(c.get("message", "")))
            for c in answer.get("checks") or []
            if isinstance(c, dict)
        ]

    def services(self, connector: Connector) -> list[HealthCheck]:
        """A GET of each declared Service's `health` path, one check per URL its Setting holds
        (a comma-separated list is several, as speech's engines). Swarmify never starts or
        stops a Service (ADR 0026); the operator runs them."""
        try:
            resolved = self._resolved(connector)
        except PermissionError:
            resolved = {}
        checks = []
        with httpx.Client(timeout=3) as http:
            for service in connector.manifest.get("services", []):
                for url in str(resolved.get(service["url"]) or "").split(","):
                    if not (url := url.strip().rstrip("/")):
                        continue
                    try:
                        http.get(url + service.get("health", "")).raise_for_status()
                        checks.append(HealthCheck(url, True, "up"))
                    except (httpx.HTTPError, httpx.InvalidURL) as exc:
                        checks.append(HealthCheck(url, False, str(exc) or type(exc).__name__))
        return checks

    # Publish stages (ADR 0007)

    def is_publish(
        self, world_id: str, config: dict[str, Any], pins: Mapping[str, str] | None = None
    ) -> bool:
        connector = self.find(world_id, str(config.get("calls", "")), pins)
        return connector is not None and connector.kind == "publish"

    def publish_preview(
        self,
        world_id: str,
        config: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        edits: dict[str, Any] | None = None,
        pins: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """What a connector stage will post, as its connector's `preview` answers it, when it
        calls a publish connector; else None. A preview that cannot be had is a problem."""
        connector = self.find(world_id, str(config.get("calls", "")), pins)
        if connector is None or connector.kind != "publish":
            return None
        args = {
            "inputs": [config["input"]]
            if config.get("input")
            else list(config.get("inputs") or []),
            "params": dict(config.get("params") or {}),
            "metadata": metadata or {},
            "edits": edits or {},
        }
        if connector.builtin:
            shown = builtin_publish.preview(connector.name, args)
        else:
            try:
                raw = self._tool(connector, "preview", args, self._env(connector))
            except ConnectorError as exc:
                raw = {"fields": [], "problems": [f"{connector.name} preview failed: {exc}"]}
            shown = publish.checked_preview(raw)
        # The gate's edits stand even when a connector forgets to apply them.
        for f in shown["fields"]:
            if f["editable"] and f["key"] in (edits or {}):
                f["value"] = (edits or {})[f["key"]]
        return {"connector": connector.name, **shown}

    def stage_problems(self, world_id: str, config: dict[str, Any]) -> list[str]:
        """What would stop a connector stage, found when its workflow is checked rather than
        after the stages before it have spent the run's budget."""
        connector = self.find(world_id, str(config.get("calls", "")))
        if connector is None:
            return []
        params = dict(config.get("params") or {})
        if connector.kind == "publish":
            if connector.builtin:
                shown = self.publish_preview(world_id, config)
                return shown["problems"] if shown else []
            # A package previews at the gate, when its inputs exist; its Manifest is checked now.
        if connector.builtin and connector.name == "remotion_render":
            out = []
            project = str(params.get("project") or "").strip()
            if not project:
                out.append("remotion_render needs params.project, the Remotion project folder")
            elif not (folder := remotion.project_folder(project, self.data_dir)).is_dir():
                where = "" if Path(project).expanduser().is_absolute() else f" in {folder.parent}"
                out.append(f"the Remotion project {project} does not exist{where}")
            if not str(params.get("composition") or "").strip():
                out.append("remotion_render needs params.composition, the composition ID")
            return out
        if connector.builtin and connector.name == "remotion_build":
            script = config.get("input") or config.get("inputs")
            out = [] if script else ["remotion_build needs an input, the script"]
            if params.get("base"):
                try:
                    remotion.template(str(params["base"]), self.data_dir)
                except ValueError as exc:
                    out.append(str(exc))
            return out
        manifest = connector.manifest  # a built-in's or an installed package's
        return _manifest_problems(manifest, config, params) if manifest else []

    def publish_result(self, call: Call) -> dict[str, Any]:
        """A finished publish call's links, normalized across connectors."""
        return publish.result(call.connector, call.response or {})

    # Outcomes

    def youtube_views(self, world_id: str, video_ids: list[str]) -> dict[str, int]:
        """View counts by video id from the `youtube` connector's `views` tool, the scheduled
        outcome write-back. Raises ConnectorError, as when its credentials are missing."""
        connector = self.find(world_id, "youtube")
        if connector is None or not video_ids:
            return {}
        answer = self._tool(connector, "views", {"video_ids": video_ids}, self._env(connector))
        counts = answer.get("views")
        if not isinstance(counts, dict):
            raise ConnectorError(f"youtube views answered without counts: {answer}")
        return {str(k): int(v) for k, v in counts.items()}

    # Connector stages

    def call_for(self, stage_id: str) -> Call | None:
        return self.storage.get_call(stage_id)

    def run_stage(
        self,
        world_id: str,
        run_id: str,
        stage_id: str,
        config: dict[str, Any],
        visit: int = 0,
        pins: Mapping[str, str] | None = None,
    ) -> Call:
        """Call the stage's connector once per visit to the stage, `visit` being the run's send-back
        count, at the package version `pins` (the workflow version's) names. A finished call is
        returned as recorded; a long job interrupted by a crash resumes polling instead of calling
        again. A call from an earlier visit is replaced: the stage reruns after a send-back."""
        done = self.storage.get_call(stage_id)
        if done is not None and done.visit != visit:
            done = None
        if done is not None and done.status == "done":
            return done
        name = str(config.get("calls", ""))
        connector = self.find(world_id, name, pins)
        if connector is None:
            raise ConnectorError(f"no connector named '{name}' in this world")
        if "connector" not in connector.stage_types:
            raise ConnectorError(f"connector '{name}' is not allowed in connector stages")
        if not connector.has_server:
            raise ConnectorError(f"connector '{name}' has no MCP server installed yet")
        if connector.kind == "publish":
            if done is not None and done.status == "running":
                # A crash mid-call: the post may be out already. Never risk posting twice.
                raise ConnectorError(
                    f"an earlier {name} call was interrupted and may have posted; check"
                    f" {name} and start a new run if it did not"
                )
            if not isinstance(config.get("confirmed"), dict):
                raise ConnectorError(f"{name} publishes only the values a gate confirmed")
        env = self._env(connector)
        call = done
        if call is None or call.status != "polling":
            call = self._call(connector, world_id, run_id, stage_id, visit, config, env, call)
        while call.status == "polling":
            time.sleep(self.poll_seconds)
            call = self._poll(connector, call, env)
        return call

    def _call(
        self,
        connector: Connector,
        world_id: str,
        run_id: str,
        stage_id: str,
        visit: int,
        config: dict[str, Any],
        env: dict[str, str],
        before: Call | None,
    ) -> Call:
        params = dict(config.get("params") or {})
        tool = str(params.pop("tool", connector.name))
        # a new key per visit, so a connector that dedupes on it does the rework
        idempotency_key = f"{stage_id}.{visit}" if visit else stage_id
        request: dict[str, Any] = {**params, "idempotency_key": idempotency_key}
        for key in ("input", "inputs", "output"):
            if config.get(key):
                request[key] = config[key]
        if connector.kind == "publish":
            values = config["confirmed"]
            if connector.builtin:
                tool, request = builtin_publish.request(
                    connector.name, values, params, idempotency_key
                )
            else:
                tool, request = "publish", {"fields": values, "idempotency_key": idempotency_key}
                request.update({k: params[k] for k in ("input_path", "input_paths") if k in params})
        now = _now()
        call = self.storage.save_call(
            Call(
                stage_id,
                world_id,
                run_id,
                connector.name,
                tool,
                request,
                "running",
                before.started_at if before else now,
                now,
                attempts=(before.attempts if before else 0) + 1,
                visit=visit,
            )
        )
        try:
            response = self._tool(connector, tool, request, env)
        except Exception as exc:
            self._fail(call, str(exc))
            raise
        if response.get("job_id") and response.get("poll"):
            return self.storage.save_call(
                replace(
                    call,
                    status="polling",
                    response=response,
                    job_id=str(response["job_id"]),
                    poll_tool=str(response["poll"]),
                    progress=_progress(response.get("progress"), 0.0),
                    message=response.get("message"),
                    updated_at=_now(),
                )
            )
        call = replace(call, response=response, updated_at=_now())
        if connector.kind == "publish":
            self._check_posted(connector, call, publish.result(connector.name, response))
        return self.storage.save_call(replace(call, status="done", progress=1.0))

    def _check_posted(self, connector: Connector, call: Call, out: dict[str, Any]) -> None:
        if not out["links"] and out["status"] != "scheduled" and out["failed"]:
            error = "; ".join(f"{p}: {e}" for p, e in out["failed"].items())
            self._fail(call, error)
            raise ConnectorError(f"{connector.name}: nothing was posted. {error}")

    def _poll(self, connector: Connector, call: Call, env: dict[str, str]) -> Call:
        assert call.poll_tool is not None and call.job_id is not None
        try:
            answer = self._tool(connector, call.poll_tool, {"job_id": call.job_id}, env)
        except Exception as exc:
            self._fail(call, str(exc))
            raise
        call = replace(
            call,
            response=answer,
            polls=call.polls + 1,
            progress=_progress(answer.get("progress"), call.progress),
            message=answer.get("message", call.message),
            updated_at=_now(),
        )
        status = answer.get("status")
        if status == "failed":
            error = str(answer.get("error") or "the job failed")
            self._fail(call, error)
            raise ConnectorError(f"{connector.name}: {error}")
        if status == "done":
            if connector.kind == "publish":  # its last answer carries what was posted
                self._check_posted(connector, call, publish.result(connector.name, answer))
            call = replace(call, status="done", progress=1.0)
        return self.storage.save_call(call)

    def _fail(self, call: Call, error: str) -> None:
        self.storage.save_call(replace(call, status="failed", error=error, updated_at=_now()))
