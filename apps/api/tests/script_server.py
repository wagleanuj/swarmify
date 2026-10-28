"""A stdio MCP connector for tests that writes a script artifact to its stage's output path."""

from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("script_writer")


@mcp.tool(description="Write a short script to the stage's output file.")
def script_writer(
    output_path: str, idempotency_key: str = "", frontmatter: dict | None = None
) -> dict[str, Any]:
    with open(output_path, "w") as f:
        f.write(f"# Why the chase works\n\nHook, then the reveal.\n\n<!-- {idempotency_key} -->\n")
    return {"output": output_path}


if __name__ == "__main__":
    mcp.run("stdio")
