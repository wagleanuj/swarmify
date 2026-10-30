"""Connectors context: the only way the swarm touches anything outside the box.

A Connector is an MCP server plus its credentials plus the stage types allowed to use it. Each
world adds its own; the built-ins need no setup; an installed Connector Package gives a world a
Connector, named as the package, only once the world enables it at a pinned version (ADR 0023).
A connector stage calls one Tool through the ConnectorTransport port with the stage id as its
Idempotency Key, records the raw call and response, and polls a long job until it ends,
appending a `connector_call.updated` event per change so the timeline shows progress.

A long job is a tool answer carrying `job_id` and `poll`, the tool to poll with
`{"job_id": ...}`; each poll answers `status` (running, done or failed) and optionally
`progress` (0 to 1), `message` and `error`.
"""

import logging
import os
import re
import shlex
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from swarmify_connectors import builtin_publish, publish, remotion, speech
from swarmify_connectors.credentials import NAME as CREDENTIAL_NAME
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
    InstallError,
    ManifestError,
    Package,
    Packages,
    read_manifest,
)
from swarmify_connectors.ports import ConnectorTransport, Storage
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
    "SqliteStorage",
    "Storage",
    "Tool",
    "from_manifest",
    "read_manifest",
]

log = logging.getLogger(__name__)
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
        {
            "name": "upload_post",
            "label": "Upload-Post",
            "kind": "publish",
            "stage_types": ["connector"],
            "about": "Posts or schedules to YouTube, TikTok and Instagram through Upload-Post's"
            " MCP.",
            "command": ["npx", "-y", "@upload-post/mcp@0.11.4"],
            "credentials": ["UPLOAD_POST_API_KEY"],
            "params": {
                "type": "object",
                "required": ["user"],
                "properties": {
                    **_PUBLISH_PARAMS,
                    "user": {"type": "string", "description": "the Upload-Post profile"},
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["youtube"],
                        "description": "where to post: youtube, tiktok, instagram",
                    },
                },
            },
            "inputs": [_VIDEO_IN],
        },
        speech.MANIFEST,
    )
}

BUILTINS = [from_manifest(m) for m in MANIFESTS.values()]


# The cheap read-only tool each built-in publish connector proves its keys with.
KEY_CHECKS = {"youtube": "channel", "upload_post": "get_account_info"}


def _account(connector: str, answer: dict[str, Any]) -> str:
    if connector == "youtube" and answer.get("channel"):
        return f"Posts to the YouTube channel {answer['channel']}."
    account = answer.get("result")
    if isinstance(account, dict) and account.get("email"):
        return f"Signed in to Upload-Post as {account['email']}."
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
    if schema.get("additionalProperties") is False:
        allowed = {*(schema.get("properties") or {}), "tool"}
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

    # Registry

    def list_all(self, world_id: str) -> list[Connector]:
        """Built-ins first, then the packages the world enables by name, then the world's own in
        the order they were added."""
        enabled = self.storage.enabled_packages(world_id).items()
        packaged = [self.package_connector(world_id, n, v) for n, v in enabled]
        return [*BUILTINS, *(c for c in packaged if c), *self.storage.list_connectors(world_id)]

    # Connector Packages a world enables (ADR 0023)

    def package_connector(self, world_id: str, name: str, version: str) -> Connector | None:
        """The Connector an installed package version gives a world, read from its Manifest;
        None, logged, when that version is gone or its Manifest no longer passes."""
        try:
            m = self.packages.manifest(name, version)
        except (InstallError, ManifestError) as exc:
            log.warning("%s enables %s %s, which is unusable: %s", world_id, name, version, exc)
            return None
        # ponytail: no Credentials yet; the package's Setup values reach it with #209.
        return Connector(
            f"package_{name}",
            world_id,
            name,
            m["kind"],
            m.get("command", []),
            m.get("url"),
            stage_types=m["stage_types"],
            about=m["about"],
            types=m.get("types", {}),
            input_types=m.get("input_types", []),
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
        if any(c.name == name for c in self.storage.list_connectors(world_id)):
            raise ValueError(f"this world has its own connector named '{name}': remove it first")
        try:
            self.packages.manifest(name, version)
        except (InstallError, ManifestError) as exc:
            raise ValueError(str(exc)) from exc
        self.storage.enable_package(world_id, name, version, _now())

    def disable(self, world_id: str, name: str) -> bool:
        """Stop the world using the package; other worlds keep theirs. False if not enabled."""
        return self.storage.disable_package(world_id, name)

    def find(self, world_id: str, name: str) -> Connector | None:
        return next((c for c in self.list_all(world_id) if c.name == name), None)

    def type_schema(self, world_id: str, name: str) -> dict[str, Any] | None:
        """The JSON Schema of type `name` as the world's connectors declare it for this world;
        None when none does, or two declare it differently (the workflow check reports that)."""
        found = [
            self.for_world(world_id, c)[0][name] for c in self.list_all(world_id) if name in c.types
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
            answer = self.transport.call(
                connector, connector.world_types, args, self._env(connector)
            )
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
        """A built-in or a world's own connector; a package's has no id outside its world."""
        builtin = next((c for c in BUILTINS if c.id == connector_id), None)
        return builtin or self.storage.get_connector(connector_id)

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
        """A validated, unsaved connector. Raises ValueError naming the first problem."""
        name, server = name.strip(), server.strip()
        if not NAME.match(name):
            raise ValueError("a name is lower_snake_case, as workflows call it: fal_video")
        if any(c.name == name for c in BUILTINS):
            raise ValueError(f"'{name}' is a built-in connector")
        if name in AGENT_TOOLS:
            raise ValueError(f"'{name}' is an Agent Tool the core gives swarm stages")
        if name in self.storage.enabled_packages(world_id):
            raise ValueError(f"'{name}' is a Connector Package this world enables")
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        if not server:
            raise ValueError("give an MCP command or a URL")
        url = server if server.startswith(("http://", "https://")) else None
        try:
            command = [] if url else shlex.split(server)
        except ValueError as exc:
            raise ValueError(f"cannot read the command: {exc}") from exc
        bad = [n for n in credentials if not CREDENTIAL_NAME.match(n)]
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
        """Start the server once with its credentials and return its tools. Saves nothing."""
        return self.transport.list_tools(connector, self._env(connector, secrets))

    def add(self, connector: Connector, secrets: dict[str, str]) -> Connector:
        """Save the connector; non-empty `secrets` are merged into the credentials file."""
        self.credentials.read()  # surface a bad file mode before anything is saved
        self.storage.add_connector(connector)
        values = {k: v for k, v in secrets.items() if v and k in connector.credentials}
        if values:
            self.credentials.set(values)
        return connector

    def delete(self, connector_id: str) -> bool:
        return self.storage.delete_connector(connector_id)

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
            answer = self.transport.call(connector, tool, {}, self._env(connector))
        except ConnectorError as exc:
            return KeyCheck(False, str(exc))
        return KeyCheck(True, _account(connector.name, answer))

    def missing(self, connector: Connector) -> list[str]:
        """Credential names the file does not hold yet."""
        try:
            have = self.credentials.read()
        except PermissionError:
            have = {}
        return [n for n in connector.credentials if not have.get(n)]

    def delete_world(self, world_id: str) -> None:
        self.storage.delete_world(world_id)

    def secret_names(self, world_id: str) -> list[str]:
        """Every key name a coding session must not see: those in the file and those any of
        the world's connectors declares."""
        try:
            saved = set(self.credentials.read())
        except PermissionError:
            saved = set()
        return sorted(saved | {n for c in self.list_all(world_id) for n in c.credentials})

    def agent_servers(self, world_id: str, names: list[str]) -> dict[str, dict[str, Any]]:
        """The standard MCP server config of each named connector that has a server, with its
        credentials: what a swarm stage's agents connect to. Raises ConnectorError when one is
        missing a credential."""
        servers: dict[str, dict[str, Any]] = {}
        for name in names:
            connector = self.find(world_id, name)
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

    def _env(self, connector: Connector, extra: dict[str, str] | None = None) -> dict[str, str]:
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
        manifest = MANIFESTS.get(connector.name) if connector.builtin else None
        for setting in (manifest or {}).get("settings", []):  # non-secret, from the app's env
            if os.environ.get(setting):
                env[setting] = os.environ[setting]
        return env

    def health(self, connector: Connector) -> list[HealthCheck]:
        """What the connector's `health` tool finds, as a speech connector's engines. Empty for
        one without a health tool. Raises ConnectorError when the tool cannot be called."""
        if not connector.health:
            return []
        answer = self.transport.call(connector, connector.health, {}, self._env(connector))
        return [
            HealthCheck(str(c.get("name", "")), bool(c.get("ok")), str(c.get("message", "")))
            for c in answer.get("checks") or []
            if isinstance(c, dict)
        ]

    # Publish stages (ADR 0007)

    def is_publish(self, world_id: str, config: dict[str, Any]) -> bool:
        connector = self.find(world_id, str(config.get("calls", "")))
        return connector is not None and connector.kind == "publish"

    def publish_preview(
        self,
        world_id: str,
        config: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        edits: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """What a connector stage will post, as its connector's `preview` answers it, when it
        calls a publish connector; else None. A preview that cannot be had is a problem."""
        connector = self.find(world_id, str(config.get("calls", "")))
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
                raw = self.transport.call(connector, "preview", args, self._env(connector))
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
            # A world's own connector previews at the gate, when its inputs exist.
            shown = self.publish_preview(world_id, config) if connector.builtin else None
            return shown["problems"] if shown else []
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
        manifest = MANIFESTS.get(connector.name) if connector.builtin else None
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
        answer = self.transport.call(
            connector, "views", {"video_ids": video_ids}, self._env(connector)
        )
        counts = answer.get("views")
        if not isinstance(counts, dict):
            raise ConnectorError(f"youtube views answered without counts: {answer}")
        return {str(k): int(v) for k, v in counts.items()}

    # Connector stages

    def call_for(self, stage_id: str) -> Call | None:
        return self.storage.get_call(stage_id)

    def run_stage(
        self, world_id: str, run_id: str, stage_id: str, config: dict[str, Any], visit: int = 0
    ) -> Call:
        """Call the stage's connector once per visit to the stage, `visit` being the run's
        send-back count. A finished call is returned as recorded; a long job interrupted by a
        crash resumes polling instead of calling again. A call from an earlier visit is
        replaced: the stage reruns after a send-back."""
        done = self.storage.get_call(stage_id)
        if done is not None and done.visit != visit:
            done = None
        if done is not None and done.status == "done":
            return done
        name = str(config.get("calls", ""))
        connector = self.find(world_id, name)
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
            response = self.transport.call(connector, tool, request, env)
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
            out = publish.result(connector.name, response)
            if out["status"] == "pending":
                # Upload-Post moved it to the background: poll its status for the links.
                return self.storage.save_call(
                    replace(call, status="polling", job_id=out["id"], poll_tool="get_status")
                )
            self._check_posted(connector, call, out)
        return self.storage.save_call(replace(call, status="done", progress=1.0))

    def _check_posted(self, connector: Connector, call: Call, out: dict[str, Any]) -> None:
        if not out["links"] and out["status"] != "scheduled" and out["failed"]:
            error = "; ".join(f"{p}: {e}" for p, e in out["failed"].items())
            self._fail(call, error)
            raise ConnectorError(f"{connector.name}: nothing was posted. {error}")

    def _poll(self, connector: Connector, call: Call, env: dict[str, str]) -> Call:
        assert call.poll_tool is not None and call.job_id is not None
        background = connector.name == "upload_post"  # an upload Upload-Post moved off
        args = {"requestId": call.job_id} if background else {"job_id": call.job_id}
        try:
            answer = self.transport.call(connector, call.poll_tool, args, env)
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
        if background:
            out = publish.result(connector.name, answer)
            if out["status"] == "pending":
                return self.storage.save_call(call)
            self._check_posted(connector, call, out)
            return self.storage.save_call(replace(call, status="done", progress=1.0))
        status = answer.get("status")
        if status == "failed":
            error = str(answer.get("error") or "the job failed")
            self._fail(call, error)
            raise ConnectorError(f"{connector.name}: {error}")
        if status == "done":
            call = replace(call, status="done", progress=1.0)
        return self.storage.save_call(call)

    def _fail(self, call: Call, error: str) -> None:
        self.storage.save_call(replace(call, status="failed", error=error, updated_at=_now()))
