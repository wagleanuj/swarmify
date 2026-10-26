"""A scripted `claude` CLI behind the Agent SDK's Transport seam, in the SDK's own test style.

The SDK talks to it exactly as to the real CLI: the initialize handshake registers the hooks,
and each scripted tool call fires the PreToolUse hook callbacks over the control protocol,
then reaches in-process tools as `mcp_message` JSON-RPC. So hooks, tools and the session's
messages all run for real, only the model's choices are scripted.

What it emulates of the CLI, and nothing more: the init message lists the skills found under
each local plugin's `skills/<name>/SKILL.md` as `<plugin>:<name>`, where `<plugin>` is the
manifest name or else the folder name (an assumption about the real CLI for a manifest-less
plugin; the opt-in real-CLI smoke test is where it gets pinned) (plus `host-skill` when host
settings are on, standing in for `~/.claude/skills` leaking in), `resume` keeps the session id,
and the result carries `cost` as the session's estimated total.
"""

import contextlib
import itertools
import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport import Transport  # the SDK's documented test seam

LAST = "$last.id"
"""In a Call's input, stands for the `id` in the last successful answer that had one, so a
script can finish the task it just claimed without knowing its id."""


@dataclass(frozen=True)
class Call:
    """The model calls `tool` with `input`."""

    tool: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class Die:
    """The CLI process dies here, before any result."""


@dataclass(frozen=True)
class Answer:
    """The session's structured output (the result's `structured_output`)."""

    data: Any


Step = Call | Say | Die | Answer


@dataclass(frozen=True)
class Outcome:
    """What one scripted call got back."""

    tool: str
    is_error: bool
    text: str


class Script:
    """One step list per session, in the order sessions start (the last one repeats).
    `transport` is the factory to hand `SdkRuntime`; `sessions` keeps each session's options
    and `outcomes` every call's answer, for assertions."""

    def __init__(self, *sessions: Sequence[Step], cost: float = 0.01, cycle: bool = False) -> None:
        self.scripts: Sequence[Sequence[Step]] = list(sessions) or [[]]
        self.cost = cost
        self.cycle = cycle
        """Start over at the first step list after the last, instead of repeating it."""
        self.sessions: list[ClaudeAgentOptions] = []
        self.prompts: list[str] = []
        """The prompt each session was started with."""
        self.outcomes: list[Outcome] = []
        self.last_id: str | None = None

    def transport(self, options: ClaudeAgentOptions) -> "ScriptedTransport":
        n = len(self.sessions)
        self.sessions.append(options)
        last = len(self.scripts) - 1
        steps = self.scripts[n % len(self.scripts) if self.cycle else min(n, last)]
        return ScriptedTransport(options, steps, self)


def _matches(matcher: str | None, tool: str) -> bool:
    return matcher in (None, "", "*") or re.fullmatch(matcher, tool) is not None


def _skills(options: ClaudeAgentOptions) -> tuple[list[str], list[dict[str, Any]], list[Any]]:
    skills: list[str] = []
    plugins: list[dict[str, Any]] = []
    errors: list[Any] = []
    for i, plugin in enumerate(options.plugins):
        path = Path(plugin["path"])
        if not path.is_dir():
            errors.append(
                {
                    "plugin": f"inline[{i}]",
                    "type": "path-not-found",
                    "message": f"{path} does not exist",
                    "path": str(path),
                }
            )
            continue
        manifest = path / ".claude-plugin" / "plugin.json"
        name = json.loads(manifest.read_text())["name"] if manifest.is_file() else path.name
        plugins.append({"name": name, "path": str(path)})
        skills += [f"{name}:{s.parent.name}" for s in sorted(path.glob("skills/*/SKILL.md"))]
    if options.setting_sources is None or "user" in options.setting_sources:
        skills.append("host-skill")
    return skills, plugins, errors


class ScriptedTransport(Transport):
    def __init__(self, options: ClaudeAgentOptions, steps: Sequence[Step], script: Script) -> None:
        self.options = options
        self.steps = steps
        self.script = script
        self.session_id = options.resume or f"session-{uuid.uuid4()}"
        self._send, self._inbox = anyio.create_memory_object_stream[dict[str, Any]](100)
        self._hooks: dict[str, list[dict[str, Any]]] = {}
        self._mcp_ready: set[str] = set()
        self._ids = itertools.count(1)
        self._ready = False

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._send.send(json.loads(line))

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None

    async def close(self) -> None:
        self._ready = False
        self._send.close()

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        async for msg in self._inbox:
            if msg["type"] == "control_request":
                if msg["request"]["subtype"] == "initialize":
                    self._hooks = msg["request"].get("hooks") or {}
                yield _answer(msg["request_id"], {})
            elif msg["type"] == "user":
                content = msg["message"]["content"]
                self.script.prompts.append(content if isinstance(content, str) else str(content))
                async for out in self._play():
                    yield out

    async def _reply(self, request_id: str) -> dict[str, Any]:
        """Wait for the SDK's answer to one of our control requests."""
        async for msg in self._inbox:
            response = msg.get("response") or {}
            if msg["type"] == "control_response" and response.get("request_id") == request_id:
                if response.get("subtype") == "error":
                    raise RuntimeError(response.get("error"))
                return response.get("response") or {}
        raise RuntimeError("the SDK closed the session mid-call")

    def _request(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        request_id = f"cli_{next(self._ids)}"
        return request_id, {"type": "control_request", "request_id": request_id, "request": request}

    async def _play(self) -> AsyncIterator[dict[str, Any]]:
        sid = self.session_id
        skills, plugins, errors = _skills(self.options)
        init: dict[str, Any] = {
            "type": "system",
            "subtype": "init",
            "uuid": str(uuid.uuid4()),
            "session_id": sid,
            "cwd": str(self.options.cwd),
            "model": "scripted",
            "permissionMode": self.options.permission_mode,
            "tools": list(self.options.allowed_tools),
            "mcp_servers": [{"name": n, "status": "connected"} for n in self.options.mcp_servers],  # type: ignore[union-attr]
            "skills": skills,
            "plugins": plugins,
        }
        if errors:
            init["plugin_errors"] = errors
        yield init
        last, answer = "", None
        for step in self.steps:
            if isinstance(step, Die):
                raise RuntimeError("the claude process died")
            if isinstance(step, Answer):
                answer = step.data
                continue
            if isinstance(step, Say):
                last = step.text
                yield _assistant(sid, {"type": "text", "text": step.text})
                continue
            if LAST in step.input.values():
                step = Call(
                    step.tool,
                    {k: self.script.last_id if v == LAST else v for k, v in step.input.items()},
                )
            use_id = f"toolu_{next(self._ids)}"
            yield _assistant(
                sid, {"type": "tool_use", "id": use_id, "name": step.tool, "input": step.input}
            )
            denied: str | None = None
            for matcher in self._hooks.get("PreToolUse", []):
                if not _matches(matcher.get("matcher"), step.tool):
                    continue
                for callback in matcher["hookCallbackIds"]:
                    hook_input = {
                        "hook_event_name": "PreToolUse",
                        "session_id": sid,
                        "transcript_path": "",
                        "cwd": str(self.options.cwd),
                        "tool_name": step.tool,
                        "tool_input": step.input,
                        "tool_use_id": use_id,
                    }
                    rid, req = self._request(
                        {
                            "subtype": "hook_callback",
                            "callback_id": callback,
                            "input": hook_input,
                            "tool_use_id": use_id,
                        }
                    )
                    yield req
                    out = (await self._reply(rid)).get("hookSpecificOutput") or {}
                    if out.get("permissionDecision") == "deny":
                        denied = out.get("permissionDecisionReason") or "denied by a hook"
            if denied is not None:
                text, is_error = denied, True
            else:
                text, is_error = "", False
                async for item in self._run_tool(step):
                    if isinstance(item, tuple):
                        text, is_error = item
                    else:
                        yield item
            self.script.outcomes.append(Outcome(step.tool, is_error, text))
            if not is_error:
                with contextlib.suppress(ValueError):
                    data = json.loads(text)
                    if isinstance(data, dict) and isinstance(data.get("id"), str):
                        self.script.last_id = data["id"]
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": use_id,
                            "content": text,
                            "is_error": is_error,
                        }
                    ],
                },
                "parent_tool_use_id": None,
                "session_id": sid,
            }
        yield {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": len(self.steps),
            "session_id": sid,
            "total_cost_usd": self.script.cost,
            "result": last,
            "structured_output": answer,
        }

    async def _run_tool(self, step: Call) -> AsyncIterator[dict[str, Any] | tuple[str, bool]]:
        """Yields control requests to the SDK, then one (text, is_error) answer."""
        parts = step.tool.split("__", 2)
        servers: dict[str, Any] = self.options.mcp_servers  # type: ignore[assignment]
        server = parts[1] if len(parts) == 3 and parts[0] == "mcp" else None
        if server is None or (servers.get(server) or {}).get("type") != "sdk":
            yield (f"{step.tool} ran", False)
            return
        messages: list[dict[str, Any]] = []
        if server not in self._mcp_ready:
            self._mcp_ready.add(server)
            messages += [
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "scripted-cli", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ]
        call_id = next(self._ids)
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": parts[2], "arguments": step.input},
            }
        )
        answer: dict[str, Any] = {}
        for message in messages:
            rid, req = self._request(
                {"subtype": "mcp_message", "server_name": server, "message": message}
            )
            yield req
            answer = (await self._reply(rid)).get("mcp_response") or {}
        if "error" in answer:
            yield (str(answer["error"].get("message")), True)
            return
        result = answer.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content") or [])
        yield (text, bool(result.get("isError")))


def _answer(request_id: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request_id, "response": response},
    }


def _assistant(session_id: str, block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": "scripted", "content": [block]},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }
