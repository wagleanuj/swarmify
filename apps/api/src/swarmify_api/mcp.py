"""The swarmify MCP server: the same actions as the HTTP API, for Claude Code and the planner.

Run over stdio with `uv run swarmify-mcp`. Reads the same `SWARMIFY_DATA_DIR` as the API, so a
world created here appears on the API's event stream.
"""

from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from swarmify_workflows import Invalid, Workflows
from swarmify_worlds import Worlds


def server(worlds: Worlds, workflows: Workflows) -> MCPServer:
    mcp = MCPServer("swarmify", instructions="Drive swarmify: worlds and workflows now, runs next.")

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
        workflows.delete_world(world_id)
        return {"deleted": world_id}

    @mcp.tool(
        description="Save workflow YAML. Without workflow_id this creates a workflow at version 1;"
        " with it, a new immutable version. Returns the version, or the violations when refused."
    )
    def save_workflow(world_id: str, yaml: str, workflow_id: str | None = None) -> dict[str, Any]:
        if worlds.get(world_id) is None:
            raise ValueError(f"world not found: {world_id}")
        try:
            return asdict(workflows.save(world_id, yaml, workflow_id))
        except Invalid as exc:
            return {"violations": [asdict(v) for v in exc.violations]}
        except KeyError as exc:
            raise ValueError(f"workflow not found in this world: {workflow_id}") from exc

    @mcp.tool(description="Check workflow YAML against every rule without saving it.")
    def check_workflow(world_id: str, yaml: str, workflow_id: str | None = None) -> dict[str, Any]:
        draft = workflows.check(world_id, yaml, workflow_id)
        return {
            "violations": [asdict(v) for v in draft.violations],
            "model": asdict(draft.model) if draft.model else None,
        }

    @mcp.tool(description="List a world's workflows with their current version numbers.")
    def list_workflows(world_id: str) -> list[dict[str, Any]]:
        return [asdict(w) for w in workflows.list_all(world_id)]

    @mcp.tool(description="Get a workflow version: its YAML and parsed stages. Latest by default.")
    def get_workflow(workflow_id: str, version: int | None = None) -> dict[str, Any]:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found: {workflow_id}")
        found = workflows.version(workflow_id, version or workflow.current_version)
        if found is None:
            raise ValueError(f"version not found: {workflow_id} v{version}")
        return asdict(found)

    return mcp


def main() -> None:
    from swarmify_api.main import data_dir, workflow_service, world_service

    root = data_dir()
    server(world_service(root), workflow_service(root)).run("stdio")
