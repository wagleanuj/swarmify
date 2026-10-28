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
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmify_connectors import publish
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


def _builtin(
    name: str,
    kind: Kind,
    stage_type: str,
    about: str,
    command: list[str] | None = None,
    credentials: list[str] | None = None,
) -> Connector:
    return Connector(
        f"builtin_{name}",
        None,
        name,
        kind,
        command or [],
        credentials=credentials or [],
        stage_types=[stage_type],
        builtin=True,
        about=about,
    )


BUILTINS = [
    _builtin("web_search", "tool", "swarm", "Built into Claude. No key."),
    _builtin("graph", "tool", "swarm", "This world's graph, read only."),
    _builtin(
        "remotion_render",
        "stage",
        "connector",
        "Renders a script with the Remotion CLI. Needs Node and a Remotion project.",
        [sys.executable, "-m", "swarmify_connectors.remotion"],
    ),
    _builtin(
        "youtube",
        "publish",
        "connector",
        "Uploads to YouTube through the Data API; a schedule keeps it private until then.",
        [sys.executable, "-m", "swarmify_connectors.youtube"],
        ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
    ),
    _builtin(
        "upload_post",
        "publish",
        "connector",
        "Posts or schedules to YouTube, TikTok and Instagram through Upload-Post's MCP.",
        ["npx", "-y", "@upload-post/mcp@0.11.4"],
        ["UPLOAD_POST_API_KEY"],
    ),
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

    def set_credentials(self, connector: Connector, values: dict[str, str]) -> None:
        """Merge values for the connector's own credential names; others are refused."""
        foreign = sorted(set(values) - set(connector.credentials))
        if foreign:
            raise ValueError(f"{connector.name} does not use {', '.join(foreign)}")
        filled = {k: v for k, v in values.items() if v}
        if filled:
            self.credentials.set(filled)

    def missing(self, connector: Connector) -> list[str]:
        """Credential names the file does not hold yet."""
        try:
            have = self.credentials.read()
        except PermissionError:
            have = {}
        return [n for n in connector.credentials if not have.get(n)]

    def delete_world(self, world_id: str) -> None:
        self.storage.delete_world(world_id)

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
        return {n: values[n] for n in connector.credentials}

    # Publish stages (ADR 0007)

    def publish_preview(self, world_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
        """What a connector stage will post, when it calls a publish connector; else None."""
        connector = self.find(world_id, str(config.get("calls", "")))
        if connector is None or connector.kind != "publish":
            return None
        return publish.preview(connector.name, config)

    def stage_problems(self, world_id: str, config: dict[str, Any]) -> list[str]:
        """What would stop a connector stage, found when its workflow is checked rather than
        after the stages before it have spent the run's budget."""
        connector = self.find(world_id, str(config.get("calls", "")))
        if connector is None:
            return []
        params = dict(config.get("params") or {})
        if connector.kind == "publish":
            if params.get("metadata") and not params.get("title"):
                # the title comes from the metadata file's frontmatter at run time; the gate
                # checks it then
                params["title"] = "from metadata"
            return publish.preview(connector.name, {**config, "params": params})["problems"]
        if connector.builtin and connector.name == "remotion_render":
            out = []
            project = str(params.get("project") or "").strip()
            if not project:
                out.append("remotion_render needs params.project, the Remotion project folder")
            elif not Path(project).expanduser().is_dir():
                out.append(f"the Remotion project {project} does not exist")
            if not str(params.get("composition") or "").strip():
                out.append("remotion_render needs params.composition, the composition ID")
            return out
        return []

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
            problems = publish.preview(name, config)["problems"]
            if problems:
                raise ConnectorError("; ".join(problems))
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
        for key in ("input", "output"):
            if config.get(key):
                request[key] = config[key]
        if connector.kind == "publish" and connector.builtin:
            tool, request = publish.request(connector.name, config, idempotency_key) or (
                tool,
                request,
            )
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
