"""The probe package's MCP server: its Setup test and its own environment."""

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("probe")


@mcp.tool(description="Pass when the API key is good.")
def check_setup() -> dict[str, Any]:
    if os.environ.get("PROBE_KEY") == "good":
        return {"ok": True, "message": "Signed in as probe."}
    return {"ok": False, "message": "The probe service refused that key."}


@mcp.tool(description="Answer with this process's environment.")
def environ(idempotency_key: str = "") -> dict[str, Any]:
    return {"env": dict(os.environ)}


@mcp.tool(description="Fail as a tool does when the provider refuses its access token.")
def refused(idempotency_key: str = "") -> dict[str, Any]:
    raise ToolError("auth_failed: the provider refused the access token")


if __name__ == "__main__":
    mcp.run("stdio")
