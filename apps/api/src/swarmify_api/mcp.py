"""The swarmify MCP server: the same actions as the HTTP API, for Claude Code and the planner.

Run over stdio with `uv run swarmify-mcp`. Reads the same `SWARMIFY_DATA_DIR` as the API, so a
world created here appears on the API's event stream.
"""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_api import tasks
from swarmify_api.agents import run_artifacts
from swarmify_api.runs import pinner, start_pinned
from swarmify_memory import Memory
from swarmify_runs import Illegal, RunControl
from swarmify_swarm import OPERATOR, Refused, Storage
from swarmify_workflows import Invalid, Workflows
from swarmify_worlds import MAX_AGENTS, Worlds


def server(
    worlds: Worlds,
    workflows: Workflows,
    whiteboard: Storage,
    control: RunControl,
    delete: Callable[[str], bool] | None = None,
    memory: Memory | None = None,
    retro: bool = True,
) -> MCPServer:
    """`control` acts on runs hosted by the API process, and records the runs it starts queued
    for that process to adopt; `delete` is the composition root's world delete
    (`main.world_deleter`), the world's row and folder only when not given. With `retro`, a
    started run ends with the Retro stage, as over HTTP. A fork through `control` re-checks
    its workflow version with this server's pin."""
    delete = delete or worlds.delete
    root = control.data_dir
    memory = memory or Memory(root / "swarmify.db", root)
    pin = control.pin = pinner(workflows)
    mcp = MCPServer(
        "swarmify",
        instructions="Drive swarmify: worlds, workflows, runs (start, fork, artifacts), swarm stage"
        " whiteboards, and the gates runs wait at.",
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
        if not delete(world_id):
            raise ValueError(f"world not found: {world_id}")
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
        """The run, or the violations when a run start's workflow version fails today's check."""
        try:
            return asdict(action())
        except Invalid as exc:
            return {"violations": [asdict(v) for v in exc.violations]}
        except (KeyError, Illegal) as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool(
        description="Start a run of a workflow version, the current one by default. budget_usd"
        " overrides the workflow's Budget; agents (1 to 8) its workers per swarm stage; baseline"
        " runs every swarm stage with one agent and no graph. The run starts queued; the API"
        " process picks it up within its sweep (15 seconds). A version that fails today's"
        " workflow check is refused with its violations."
    )
    def start_run(
        workflow_id: str,
        version: int | None = None,
        budget_usd: float | None = None,
        agents: int | None = None,
        baseline: bool = False,
    ) -> dict[str, Any]:
        if budget_usd is not None and not budget_usd > 0:
            raise ValueError("budget_usd must be above 0")
        if agents is not None and not 1 <= agents <= MAX_AGENTS:
            raise ValueError(f"agents must be 1 to {MAX_AGENTS}")
        return run_or_fail(
            lambda: start_pinned(
                control, pin(workflow_id, version), retro, budget_usd, agents, baseline
            )
        )

    @mcp.tool(
        description="Fork a run: a new run of the same workflow version that copies it, notes"
        " and artifacts included, up to stage `stage` (0-based) and runs on from there. Every"
        " earlier stage must have finished. A version that fails today's workflow check is"
        " refused with its violations."
    )
    def fork_run(run_id: str, stage: int) -> dict[str, Any]:
        return run_or_fail(lambda: control.fork(run_id, stage))

    @mcp.tool(
        description="List a run's artifacts in stage order, gate decisions included, each with"
        " its path in the world folder (what decide_gate takes as `artifact`), type, title and"
        " body. With `before`, only those of the stages before that index."
    )
    def list_artifacts(run_id: str, before: int | None = None) -> list[dict[str, Any]]:
        run = control.get(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        return [asdict(a) for a in run_artifacts(memory, run, before)]

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
        description="Answer the gate a run waits at. A pick gate needs `choice` or `artifact`,"
        " the chosen artifact's path in the world folder, `runs/<run>/<stage id>/artifacts/<name>`"
        " as the API's `GET /api/runs/{run_id}/artifacts` lists it (the choice then takes its"
        " title); an approve gate may name `send_back_to`, an earlier stage that runs again"
        " with `note` in its context (at most twice per run); a confirm gate takes neither."
        " Writes a decision artifact."
    )
    def decide_gate(
        run_id: str,
        choice: str | None = None,
        note: str = "",
        send_back_to: str | None = None,
        artifact: str | None = None,
    ) -> dict[str, Any]:
        return run_or_fail(
            lambda: control.decide(run_id, choice, note, send_back_to, artifact=artifact)
        )

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


def approvals_server(control: RunControl) -> MCPServer:
    """The part of the MCP server a Remote Approval connector reaches with the token the core
    provisions for it (scope `remote_approvals`, at `/mcp/remote_approvals`): answering a gate,
    nothing else. The answer resolves exactly as one in the web UI; the first answer wins."""
    mcp = MCPServer(
        "swarmify-remote-approvals",
        instructions="Answer a gate Swarmify posted to you, as the person who answered it.",
    )

    @mcp.tool(
        description="Answer the gate a posted message is for: `run_id`, `stage_index` and `visit`"
        ' as the gate carried them, and `by`, who answered, as a person reads it ("anuj in'
        ' chat"). Then the `decide` of the answer chosen: a pick names `artifact`, an approve'
        " may name `send_back_to`. Refused once the gate was answered anywhere, or has moved on."
    )
    def decide_gate(
        run_id: str,
        stage_index: int,
        visit: int,
        by: str,
        choice: str | None = None,
        artifact: str | None = None,
        send_back_to: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        try:
            run = control.decide(
                run_id,
                choice,
                note,
                send_back_to,
                artifact=artifact,
                gate=(stage_index, visit),
                by=by.strip()[:100] or None,
            )
        except (KeyError, ValueError) as exc:  # Illegal is a ValueError
            raise ToolError(str(exc).strip("'\"")) from exc
        return {"run_id": run.id, "status": run.status}

    return mcp


def stdio_server(root: Path, control: RunControl) -> MCPServer:
    """The server `swarmify-mcp` runs over `root`, deleting worlds as the API does."""
    from swarmify_api.main import (
        connector_service,
        learning_service,
        notification_service,
        task_board,
        workflow_service,
        world_deleter,
        world_service,
    )

    worlds, registry, whiteboard = world_service(root), connector_service(root), task_board(root)
    workflows = workflow_service(root, registry, control)
    delete = world_deleter(
        worlds,
        workflows,
        control,
        registry,
        whiteboard,
        notification_service(root),
        learning_service(root),
    )
    return server(worlds, workflows, whiteboard, control, delete)


def main() -> None:
    from swarmify_api.main import data_dir, run_control

    root = data_dir()
    stdio_server(root, run_control(root)).run("stdio")
