"""The swarmify MCP server: the same actions as the HTTP API, for Claude Code and the planner.

Run over stdio with `uv run swarmify-mcp`. Reads the same `SWARMIFY_DATA_DIR` as the API, so a
world created here appears on the API's event stream.
"""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from swarmify_api import tasks
from swarmify_runs import Illegal, RunControl
from swarmify_swarm import OPERATOR, Refused, Storage
from swarmify_workflows import Invalid, Workflows
from swarmify_worlds import Worlds


def server(
    worlds: Worlds,
    workflows: Workflows,
    whiteboard: Storage,
    control: RunControl,
    on_delete: Callable[[str], None] = lambda _: None,
) -> MCPServer:
    """`control` acts on runs hosted by the API process; `on_delete` clears other contexts' rows
    of a deleted world (runs and tasks, for now)."""
    mcp = MCPServer(
        "swarmify",
        instructions="Drive swarmify: worlds, workflows, swarm stage whiteboards, and the gates"
        " runs wait at.",
    )

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
        on_delete(world_id)
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

    @mcp.tool(
        description="Add a task to a swarm stage's whiteboard to steer the swarm. It starts open;"
        " depends_on names tasks of the same stage that must be done before it can be claimed."
    )
    def add_task(
        run_id: str,
        stage_id: str,
        title: str,
        body: str = "",
        depends_on: list[str] | None = None,
        added_by: str = OPERATOR,
    ) -> dict[str, Any]:
        try:
            task = tasks.add_task(
                whiteboard,
                control.get,
                run_id,
                stage_id,
                title,
                body=body,
                depends_on=tuple(depends_on or ()),
                added_by=added_by,
            )
        except KeyError as exc:
            raise ValueError(f"stage not found in run {run_id}: {stage_id}") from exc
        except Refused as exc:
            raise ValueError(str(exc)) from exc
        return asdict(task)

    @mcp.tool(description="List the tasks on a swarm stage's whiteboard, oldest first.")
    def list_tasks(run_id: str, stage_id: str) -> list[dict[str, Any]]:
        try:
            return [asdict(t) for t in tasks.list_tasks(whiteboard, control.get, run_id, stage_id)]
        except KeyError as exc:
            raise ValueError(f"stage not found in run {run_id}: {stage_id}") from exc

    def run_or_fail(action: Callable[[], Any]) -> dict[str, Any]:
        try:
            return asdict(action())
        except (KeyError, Illegal) as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool(description="Get a run with its stages and gate decisions.")
    def get_run(run_id: str) -> dict[str, Any]:
        run = control.get(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        return asdict(run)

    @mcp.tool(description="List every open gate across all worlds and runs, newest first.")
    def list_gates() -> list[dict[str, Any]]:
        return [asdict(g) for g in control.gates()]

    @mcp.tool(
        description="Answer the gate a run waits at. A pick gate needs `choice`; an approve gate"
        " may name `send_back_to`, an earlier stage that runs again with `note` in its context"
        " (at most twice per run); a confirm gate takes neither. Writes a decision artifact."
    )
    def decide_gate(
        run_id: str, choice: str | None = None, note: str = "", send_back_to: str | None = None
    ) -> dict[str, Any]:
        return run_or_fail(lambda: control.decide(run_id, choice, note, send_back_to))

    @mcp.tool(
        description="Set a run's Budget in US dollars, the total, not an increment. It covers the"
        " run and its child runs and must be above what they spent. A run stopped at a budget"
        " gate goes on; to refuse more money, abort the run instead."
    )
    def set_budget(run_id: str, budget_usd: float) -> dict[str, Any]:
        return run_or_fail(lambda: control.set_budget(run_id, budget_usd))

    @mcp.tool(description="Pause a run: at once at a gate, otherwise after the current stage.")
    def pause_run(run_id: str) -> dict[str, Any]:
        return run_or_fail(lambda: control.pause(run_id))

    @mcp.tool(description="Resume a paused run.")
    def resume_run(run_id: str) -> dict[str, Any]:
        return run_or_fail(lambda: control.resume(run_id))

    @mcp.tool(description="Abort a run and every child run it started.")
    def abort_run(run_id: str) -> dict[str, Any]:
        return run_or_fail(lambda: control.abort(run_id))

    return mcp


def main() -> None:
    from swarmify_api.main import (
        connector_service,
        data_dir,
        run_control,
        task_board,
        workflow_service,
        world_service,
    )

    root = data_dir()
    registry = connector_service(root)
    control, whiteboard = run_control(root), task_board(root)

    def on_delete(world_id: str) -> None:
        # ponytail: deletes run rows only; a run in flight in the API process is not cancelled.
        # Cancel through the control's DBOSClient when the MCP server starts runs too.
        control.storage.delete_world(world_id)
        whiteboard.delete_world(world_id)
        registry.delete_world(world_id)

    server(
        world_service(root), workflow_service(root, registry), whiteboard, control, on_delete
    ).run("stdio")
