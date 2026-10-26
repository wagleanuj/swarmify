"""The swarmify MCP server: the same actions as the HTTP API, for Claude Code and the planner.

Run over stdio with `uv run swarmify-mcp`. Reads the same `SWARMIFY_DATA_DIR` as the API, so a
world created here appears on the API's event stream.
"""

from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from swarmify_worlds import Worlds


def server(worlds: Worlds) -> MCPServer:
    mcp = MCPServer("swarmify", instructions="Drive swarmify: worlds now, workflows and runs next.")

    @mcp.tool(description="Create a world: one long-lived memory with its own folder and stream.")
    def create_world(name: str, purpose: str = "") -> dict[str, Any]:
        return asdict(worlds.create(name, purpose))

    @mcp.tool(description="List every world in creation order.")
    def list_worlds() -> list[dict[str, Any]]:
        return [asdict(w) for w in worlds.list_all()]

    @mcp.tool(description="Get one world by id.")
    def get_world(world_id: str) -> dict[str, Any]:
        world = worlds.get(world_id)
        if world is None:
            raise ValueError(f"world not found: {world_id}")
        return asdict(world)

    @mcp.tool(description="Delete a world, its folder and its rows. Irreversible.")
    def delete_world(world_id: str) -> dict[str, Any]:
        if not worlds.delete(world_id):
            raise ValueError(f"world not found: {world_id}")
        return {"deleted": world_id}

    return mcp


def main() -> None:
    from swarmify_api.main import data_dir, world_service

    server(world_service(data_dir())).run("stdio")
