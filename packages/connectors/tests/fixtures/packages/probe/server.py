"""The probe package's MCP server: its Setup test and its own environment."""

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("probe")


@mcp.tool(description="Pass when the API key is good.")
def check_setup() -> dict[str, Any]:
    if os.environ.get("PROBE_KEY") == "good":
        return {"ok": True, "message": "Signed in as probe."}
    return {"ok": False, "message": "The probe service refused that key."}


@mcp.tool(description="Answer with this process's environment.")
def environ(idempotency_key: str = "") -> dict[str, Any]:
    return {"env": dict(os.environ)}


if __name__ == "__main__":
    mcp.run("stdio")
