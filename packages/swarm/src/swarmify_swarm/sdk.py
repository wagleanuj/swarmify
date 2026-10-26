"""The Agent SDK adapter of the AgentRuntime port (ADR 0010).

Each session gets the stage's in-process tools as one SDK MCP server, a PreToolUse hook that
asks SessionHooks about every call, the world folder as a local plugin and no host settings
(ADR 0016). Without a transport factory the SDK starts its bundled `claude` CLI; tests pass one
that returns a scripted transport, which the SDK drives exactly like the CLI.
"""

from collections.abc import Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import McpServerConfig

from swarmify_swarm.ports import (
    TOOL_SERVER,
    AgentTool,
    SessionEnd,
    SessionHooks,
    SessionInit,
    SessionSpec,
    ToolError,
)

# ponytail: one price for every model, Claude Opus 5.5's per million tokens (input, output, cache
# write, cache read). The SDK prices only the whole session, at its result, and that total trues
# the per-step estimates up; a price table per model when stopping on time needs it. A resumed
# session's total includes the session it resumed, so a replacement counts that one twice: the
# Budget errs high, never low.
PRICE_PER_MTOK = {
    "input_tokens": 4.0,
    "output_tokens": 20.0,
    "cache_creation_input_tokens": 5.0,
    "cache_read_input_tokens": 0.2,
}


def step_cost(usage: dict[str, Any] | None) -> float:
    """The estimated cost of one model step from its usage."""
    return sum(float((usage or {}).get(k) or 0) * p for k, p in PRICE_PER_MTOK.items()) / 1e6


TransportFactory = Callable[[ClaudeAgentOptions], Any]
"""Builds the SDK `Transport` for a session from its options (the SDK's own test seam)."""


def _sdk_tool(spec: AgentTool) -> Any:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"content": [{"type": "text", "text": await spec.run(args)}]}
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}

    return tool(spec.name, spec.description, spec.schema)(handler)


def options(spec: SessionSpec, hooks: SessionHooks, model: str | None = None) -> ClaudeAgentOptions:
    async def pre_tool(data: Any, _tool_use_id: str | None, _context: Any) -> Any:
        reason = hooks.before_tool(data["tool_name"], data.get("tool_input") or {})
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    servers: dict[str, McpServerConfig] = {
        TOOL_SERVER: create_sdk_mcp_server(TOOL_SERVER, tools=[_sdk_tool(t) for t in spec.tools]),
        **spec.servers,  # type: ignore[dict-item]  # standard MCP config dicts
    }
    return ClaudeAgentOptions(
        system_prompt=spec.system_prompt,
        cwd=spec.cwd,
        mcp_servers=servers,
        allowed_tools=list(spec.allowed),
        permission_mode="dontAsk",
        setting_sources=[],
        plugins=[{"type": "local", "path": str(spec.plugin)}],
        resume=spec.resume,
        max_turns=spec.max_turns,
        model=model,
        output_format=(
            {"type": "json_schema", "schema": spec.output_schema} if spec.output_schema else None
        ),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool])]},
    )


class SdkRuntime:
    def __init__(self, transport: TransportFactory | None = None, model: str | None = None):
        self.transport = transport
        self.model = model

    async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
        opts = options(spec, hooks, self.model)
        custom = self.transport(opts) if self.transport else None
        spent, steps, session = 0.0, set[str](), spec.resume or ""
        async with ClaudeSDKClient(opts, transport=custom) as client:
            await client.query(spec.prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    # One model step arrives as one message per content block, each carrying the
                    # step's usage, so a step is charged once, by its message id.
                    step = msg.message_id or msg.uuid or str(len(steps))
                    if step in steps or not msg.usage:
                        continue
                    steps.add(step)
                    usd = step_cost(msg.usage)
                    spent += usd
                    if stop := hooks.cost(usd):
                        # Leaving the client ends the CLI; the session id stays resumable.
                        return SessionEnd(
                            msg.session_id or session, spent, True, stop, stopped=stop
                        )
                elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                    session = msg.data["session_id"]
                    errors = msg.data.get("plugin_errors") or []
                    hooks.started(
                        SessionInit(
                            msg.data["session_id"],
                            list(msg.data.get("skills") or []),
                            [f"{e.get('plugin')}: {e.get('message')}" for e in errors],
                        )
                    )
                elif isinstance(msg, ResultMessage):
                    cost = spent if msg.total_cost_usd is None else msg.total_cost_usd
                    hooks.cost(cost - spent)
                    return SessionEnd(
                        msg.session_id, cost, msg.is_error, msg.result or "", msg.structured_output
                    )
        raise RuntimeError("the agent session ended without a result")
