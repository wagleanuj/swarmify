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


@mcp.tool(description="Finish a sign-in (ADR 0029): keep a Credential made from the token.")
def signed_in(access_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    return {"access_token": f"page-{access_token}", "expires_at": None}


if __name__ == "__main__":
    mcp.run("stdio")
