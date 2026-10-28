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
TOOLS = ("check_workflow", "save_workflow", "get_workflow", "list_workflows")
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

A workflow is YAML: `name`, optional `schedule` (cron, UTC), `budget_usd`, `agents`, and
`stages`, a list where each stage is `- <type>: <stage name>` plus its fields:
- swarm: goal, done_when; optional tools (connector names such as web_search, graph), graphs,
  output_type (idea or list[idea]), produces (artifact file names).
- gate: ask; optional kind: pick | approve | confirm (approve by default).
- connector: calls (connector name, defaults to the stage name); optional input, output, params.
- workflow: runs (a workflow id); optional take (artifact names to copy from the child).
Rules: a publish connector (youtube, upload_post) must come right after a gate; a connector's
input must be produced by an earlier stage; connectors must exist in the world. Swarm agents
write text only, so a swarm stage produces text files (script.md, ideas.json), never video.mp4
or an image: a video comes from a remotion_render connector stage.

The built-in connector stages and their params:
- remotion_render: input is the script; params project (a template's name under the data
  directory's remotion folder, or a folder path) and composition (its ID), both required;
  optional entry, codec, frames, props.
- youtube: input is the video; params title (required unless metadata names a file an earlier
  stage produced whose frontmatter has title and description), description, visibility
  (public, unlisted or private), schedule (ISO 8601 with a timezone, or 09:00, or +1d 09:00;
  needs public), made_for_kids (true or false).
- upload_post: the same params as youtube, plus user (the Upload-Post profile, required) and
  platforms (default [youtube]).

How you work: draft the whole YAML, call check_workflow with it and fix what it reports before
you answer, then return the full YAML in `yaml` with a short reply. Ask about a detail only when
it changes the shape of the workflow. Call save_workflow only when the operator asks you to
save; pass workflow_id when editing an existing workflow."""


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


def router(worlds: Worlds, runtime: AgentRuntime, server: MCPServer) -> APIRouter:
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
            system_prompt=f"{SYSTEM}\n\nThe world's id is {world_id}.",
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
