"""Connectors context: the only way the swarm touches anything outside the box.

A Connector is an MCP server plus its credentials plus the stage types allowed to use it. Each
world adds its own; the built-ins need no setup. A connector stage calls one Tool through the
ConnectorTransport port with the stage id as its Idempotency Key, records the raw call and
response, and polls a long job until it ends, appending a `connector_call.updated` event per
change so the timeline shows progress.

A long job is a tool answer carrying `job_id` and `poll`, the tool to poll with
`{"job_id": ...}`; each poll answers `status` (running, done or failed) and optionally
`progress` (0 to 1), `message` and `error`.
"""

import re
import shlex
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

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
    Kind,
    Tool,
)
from swarmify_connectors.ports import ConnectorTransport, Storage
from swarmify_connectors.sqlite import MIGRATIONS, SqliteStorage
from swarmify_kernel import new_id

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
    "McpTransport",
    "SqliteStorage",
    "Storage",
    "Tool",
]

NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")


def _builtin(name: str, kind: Kind, stage_type: str, about: str) -> Connector:
    return Connector(
        f"builtin_{name}", None, name, kind, stage_types=[stage_type], builtin=True, about=about
    )


# ponytail: remotion_render, youtube and upload_post are declared so workflows can name them;
# their wrappers ship with the render and publish connectors. Until then a call fails clearly.
BUILTINS = [
    _builtin("web_search", "tool", "swarm", "Built into Claude. No key."),
    _builtin("graph", "tool", "swarm", "This world's graph, read only."),
    _builtin("remotion_render", "stage", "connector", "Renders a script. Server not installed."),
    _builtin("youtube", "publish", "connector", "Publishes to YouTube. Server not installed."),
    _builtin("upload_post", "publish", "connector", "Schedules posts. Server not installed."),
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _progress(value: Any, before: float | None) -> float | None:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return before


class Connectors:
    def __init__(
        self,
        storage: Storage,
        transport: ConnectorTransport,
        credentials: Credentials,
        poll_seconds: float = 2.0,
    ) -> None:
        self.storage = storage
        self.transport = transport
        self.credentials = credentials
        self.poll_seconds = poll_seconds

    # Registry

    def list_all(self, world_id: str) -> list[Connector]:
        """Built-ins first, then the world's own in the order they were added."""
        return [*BUILTINS, *self.storage.list_connectors(world_id)]

    def find(self, world_id: str, name: str) -> Connector | None:
        return next((c for c in self.list_all(world_id) if c.name == name), None)

    def get(self, connector_id: str) -> Connector | None:
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
    ) -> Connector:
        """A validated, unsaved connector. Raises ValueError naming the first problem."""
        name, server = name.strip(), server.strip()
        if not NAME.match(name):
            raise ValueError("a name is lower_snake_case, as workflows call it: fal_video")
        if any(c.name == name for c in BUILTINS):
            raise ValueError(f"'{name}' is a built-in connector")
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

    def missing(self, connector: Connector) -> list[str]:
        """Credential names the file does not hold yet."""
        try:
            have = self.credentials.read()
        except PermissionError:
            have = {}
        return [n for n in connector.credentials if not have.get(n)]

    def delete_world(self, world_id: str) -> None:
        self.storage.delete_world(world_id)

    def _env(self, connector: Connector, extra: dict[str, str] | None = None) -> dict[str, str]:
        values = {**self.credentials.read(), **{k: v for k, v in (extra or {}).items() if v}}
        missing = [n for n in connector.credentials if not values.get(n)]
        if missing:
            raise ConnectorError(
                f"{connector.name} needs {', '.join(missing)} in {self.credentials.path}"
            )
        return {n: values[n] for n in connector.credentials}

    # Connector stages

    def call_for(self, stage_id: str) -> Call | None:
        return self.storage.get_call(stage_id)

    def run_stage(self, world_id: str, run_id: str, stage_id: str, config: dict[str, Any]) -> Call:
        """Call the stage's connector once per stage id. A finished call is returned as
        recorded; a long job interrupted by a crash resumes polling instead of calling again."""
        done = self.storage.get_call(stage_id)
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
        env = self._env(connector)
        call = done
        if call is None or call.status != "polling":
            call = self._call(connector, world_id, run_id, stage_id, config, env, call)
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
        config: dict[str, Any],
        env: dict[str, str],
        before: Call | None,
    ) -> Call:
        params = dict(config.get("params") or {})
        tool = str(params.pop("tool", connector.name))
        request: dict[str, Any] = {**params, "idempotency_key": stage_id}
        for key in ("input", "output"):
            if config.get(key):
                request[key] = config[key]
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
        return self.storage.save_call(
            replace(call, status="done", response=response, progress=1.0, updated_at=_now())
        )

    def _poll(self, connector: Connector, call: Call, env: dict[str, str]) -> Call:
        assert call.poll_tool is not None and call.job_id is not None
        try:
            answer = self.transport.call(connector, call.poll_tool, {"job_id": call.job_id}, env)
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
            call = replace(call, status="done", progress=1.0)
        return self.storage.save_call(call)

    def _fail(self, call: Call, error: str) -> None:
        self.storage.save_call(replace(call, status="failed", error=error, updated_at=_now()))
