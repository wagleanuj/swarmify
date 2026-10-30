"""The planner chat of the workflow editor: a Planner session on the AgentRuntime that drafts
and edits a workflow through the swarmify MCP tools, called in-process.

One message in, one structured answer out: a reply for the operator and, when the planner
changed something, the whole workflow YAML, which becomes the editor's text (the stage list,
the YAML and the chat are projections of that one text). The session id comes back so the next
message resumes the conversation. The planner saves only when asked to; the editor otherwise
keeps the draft unsaved until the operator saves it."""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from swarmify_connectors import Connector, Connectors
from swarmify_kernel import AGENT_TOOLS
from swarmify_swarm import (
    AgentRuntime,
    AgentTool,
    SessionEnd,
    SessionInit,
    SessionSpec,
    ToolError,
    tool_id,
)
from swarmify_workflows import WorkflowVersion
from swarmify_worlds import Worlds

log = logging.getLogger(__name__)
PLANNER = "planner"
TOOLS = ("check_workflow", "save_workflow", "get_workflow", "list_workflows", "start_run")
"""The swarmify MCP tools the planner may call; the rest of the server stays out of reach."""
MAX_TURNS = 30
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "What you tell the operator, in plain words"},
        "yaml": {
            "type": ["string", "null"],
            "description": "The whole workflow YAML after your changes, or null if unchanged",
        },
    },
    "required": ["reply"],
}
SYSTEM = """You are the planner of swarmify. You turn what an operator describes into a workflow
and edit the one they have. Answer only with the JSON the schema asks for.

A workflow is YAML: `name`, optional `schedule` (cron, UTC), `budget_usd`, `agents` (the
default for swarm stages), and `stages`, a list where each stage is `- <type>: <stage name>`
plus its fields:
- swarm: goal, done_when; optional tools (Agent Tools, below, or the world's tool connectors),
  graphs, output_type (idea or list[idea], or a type a connector declares, below), produces
  (artifact file names), agents (how many workers, 1 by default; leave it at 1 unless asked).
- gate: ask; optional kind: pick | approve | confirm (approve by default); an approve gate may set
  max_send_backs, how often it may send the run back (2 by default).
- connector: calls (connector name, defaults to the stage name); optional input, output, params;
  inputs (a list of artifact names, each produced by an earlier stage) instead of input when
  the connector takes several files.
- workflow: runs (a workflow id); optional take (artifact names to copy from the child).
Rules: a publish connector must come right after a gate; a connector's input must be produced
by an earlier stage; connectors must exist in the world. Swarm agents write text only, so a swarm
stage produces text files (script.md, ideas.json), never video, audio or an image: such a file
comes from a connector stage whose output has that media type (the world's connectors, below).

The Agent Tools the core gives a swarm stage's agents; they are not connectors, so never a
connector stage:
{agent_tools}

How you work: draft the whole YAML, call check_workflow with it and fix what it reports before
you answer, then return the full YAML in `yaml` with a short reply. Ask about a detail only when
it changes the shape of the workflow. Call save_workflow only when the operator asks you to
save; pass workflow_id when editing an existing workflow. Call start_run only when the operator
asks you to run a saved workflow.""".replace(
    "{agent_tools}", "\n".join(f"- {name}: {about}." for name, about in AGENT_TOOLS.items())
)


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    yaml: str | None = Field(default=None, max_length=100_000)
    """The editor's current text, so the planner edits what the operator sees."""
    workflow_id: str | None = None
    session_id: str | None = None
    """Continues an earlier exchange; left out, the conversation starts fresh."""


class Reply(BaseModel):
    reply: str
    yaml: str | None
    session_id: str
    saved: WorkflowVersion | None = None
    """The version the planner saved during this exchange, when the operator asked it to."""


class _Hooks:
    """Allow only the planner's tools; remember the session."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed
        self.session_id: str | None = None

    def started(self, init: SessionInit) -> None:
        self.session_id = init.session_id

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        return None if tool in self.allowed else f"{tool} is not allowed in the planner chat."

    def cost(self, usd: float) -> str | None:
        return None


def _text(result: CallToolResult) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "".join(getattr(c, "text", "") for c in result.content)


async def mcp_tools(server: MCPServer, saves: list[dict[str, Any]]) -> list[AgentTool]:
    """The planner's tools, each one of the swarmify MCP server's own, called in-process.
    `saves` collects every version `save_workflow` returns."""
    tools: list[AgentTool] = []
    for tool in await server.list_tools():
        if tool.name not in TOOLS:
            continue

        def run(args: dict[str, Any], name: str = tool.name) -> Any:
            return _call(server, name, args, saves)

        tools.append(AgentTool(tool.name, tool.description or "", tool.input_schema, run))
    return tools


async def _call(server: MCPServer, name: str, args: dict[str, Any], saves: list) -> str:
    try:
        result = await server.call_tool(name, args)
    except McpToolError as exc:
        raise ToolError(str(exc)) from exc
    if not isinstance(result, CallToolResult):  # an elicitation the planner chat cannot answer
        raise ToolError(f"{name} asked for input the planner chat cannot give.")
    if result.is_error:
        raise ToolError(_text(result))
    content = result.structured_content
    if name == "save_workflow" and isinstance(content, dict) and "number" in content:
        saves.append(content)
    return _text(result)


def _prompt(body: Message) -> str:
    parts = [body.message.strip()]
    if body.workflow_id:
        parts.append(f"The workflow being edited has id {body.workflow_id}.")
    if body.yaml and body.yaml.strip():
        parts.append(f"The editor currently shows this YAML:\n```yaml\n{body.yaml.rstrip()}\n```")
    return "\n\n".join(parts)


def parse_answer(output: Any) -> tuple[str, str | None]:
    if not isinstance(output, dict) or not str(output.get("reply") or "").strip():
        raise ValueError("the planner answered without a reply")
    yaml = output.get("yaml")
    if yaml is not None and not isinstance(yaml, str):
        raise ValueError("the planner's yaml was not text")
    return str(output["reply"]).strip(), (yaml if yaml and yaml.strip() else None)


def _files(files: list[dict[str, Any]]) -> str:
    return "; ".join(f"{f['description']} ({f['media_type']})" for f in files)


def describe(connector: Connector) -> str:
    """One connector as the planner reads it, generated from its Manifest (ADR 0019): about,
    input and output files, params from the JSON Schema with their descriptions, and its planner
    notes. A connector a world added by hand has no Manifest; its own fields stand in."""
    m = connector.manifest or {"about": connector.about}
    where = " and ".join(connector.stage_types)
    lines = [f"- {connector.name} ({connector.kind}, in {where} stages): {m['about']}".rstrip()]
    for key in ("inputs", "outputs"):
        if files := m.get(key):
            label = key[:-1] if len(files) == 1 else f"{key}, in order"
            lines.append(f"  {label}: {_files(files)}")
    schema = m.get("params") or {}
    required = set(schema.get("required") or [])
    for name, param in (schema.get("properties") or {}).items():
        said = [
            "required" if name in required else "",
            f"one of {', '.join(map(str, param['enum']))}" if "enum" in param else "",
            f"default {json.dumps(param['default'])}" if "default" in param else "",
            str(param.get("description") or ""),
        ]
        lines.append(f"  params.{name}: {'; '.join(s for s in said if s)}".rstrip(": "))
    if m.get("planner_notes"):
        lines.append(f"  {m['planner_notes']}")
    return "\n".join(lines)


def world_connectors(connectors: Connectors, world_id: str) -> str:
    """Every connector the world can use, each described from its Manifest."""
    return "\n".join(
        [
            "The world's connectors, each with its kind, the stage types that may use it (a"
            " connector stage calls it; a swarm stage lists it in tools), its files, params and"
            " notes:",
            *(describe(c) for c in connectors.list_all(world_id)),
        ]
    )


def typed_inputs(connectors: Connectors, world_id: str) -> str:
    """The planner's view of the world's typed contracts (issue 133): which connector input
    takes which declared type, so it drafts `output_type` on the swarm stage that writes it."""
    lines = [
        f"- {c.name}: input {n + 1} is type {t}"
        for c in connectors.list_all(world_id)
        for n, t in enumerate(c.input_types)
        if t
    ]
    if not lines:
        return ""
    # What a connector says about its types in this world (issue 140), as the voices a world
    # has; the core passes it on unread.
    notes = [
        f"- {c.name}: {note}"
        for c in connectors.list_all(world_id)
        if c.world_types and (note := connectors.for_world(world_id, c)[1])
    ]
    return "\n".join(
        [
            "Typed inputs: a swarm stage that produces a file one of these connector inputs reads"
            " (input, or the nth of inputs) must declare that type as its output_type, as"
            " `output_type: script`; the check refuses the workflow otherwise. The judge then"
            " writes the file in the connector's shape.",
            *lines,
            *(["In this world, the connectors say:", *notes] if notes else []),
        ]
    )


def router(
    worlds: Worlds, runtime: AgentRuntime, server: MCPServer, connectors: Connectors
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["planner"])
    allowed = [tool_id(t) for t in TOOLS]

    @api.post("/worlds/{world_id}/planner", responses={404: {}, 502: {}})
    async def chat(world_id: str, body: Message) -> Reply:
        """One turn of the planner chat. 502 when the planner's session fails or does not
        answer in the shape asked; send the message again to retry."""
        world = worlds.get(world_id)
        if world is None:
            raise HTTPException(404, "world not found")
        saves: list[dict[str, Any]] = []
        folder = worlds.folder(world_id)
        spec = SessionSpec(
            system_prompt="\n\n".join(
                filter(
                    None,
                    [
                        SYSTEM,
                        world_connectors(connectors, world_id),
                        typed_inputs(connectors, world_id),
                        f"The world's id is {world_id}.",
                    ],
                )
            ),
            prompt=_prompt(body),
            cwd=folder,
            plugin=folder,
            tools=await mcp_tools(server, saves),
            allowed=allowed,
            resume=body.session_id,
            max_turns=MAX_TURNS,
            output_schema=ANSWER_SCHEMA,
            model=world.model,  # the world default; None leaves it to the global one
        )
        hooks = _Hooks(allowed)
        try:
            end: SessionEnd = await runtime.run(spec, hooks)
            if end.is_error:
                raise RuntimeError(end.result or "the session failed")
            reply, yaml = parse_answer(end.output)
        except Exception as exc:
            log.warning("planner chat failed in world %s: %s", world_id, exc)
            raise HTTPException(502, f"The planner did not answer: {exc}") from exc
        saved = saves[-1] if saves else None
        if saved is not None and yaml is None:
            yaml = str(saved["yaml"])
        sid = hooks.session_id or end.session_id
        return Reply.model_validate(dict(reply=reply, yaml=yaml, session_id=sid, saved=saved))

    return api
