"""HTTP face of the Runs context: start a run of a workflow version, list runs, read one, act
on one (decide at its gate, pause, resume, abort) and list every open gate."""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarmify_runs import Gate, Illegal, Pinned, Run, Runner, StageDef
from swarmify_workflows import Stage, Workflows
from swarmify_worlds import Worlds


class StartRun(BaseModel):
    version: int | None = None
    """The workflow version to pin. The current one when left out."""


class Decide(BaseModel):
    """A gate answer. Pick gates need `choice`; approve gates may name `send_back_to`, an earlier
    stage that then runs again with `note` in its context; confirm gates take neither."""

    choice: str | None = Field(default=None, max_length=2_000)
    note: str = Field(default="", max_length=10_000)
    send_back_to: str | None = None


ACTION_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such run"},
    409: {"description": "The run is not in a state where this action applies"},
}


def stage_def(stage: Stage) -> StageDef:
    fields = asdict(stage)
    return StageDef(fields.pop("name"), fields.pop("type"), fields)


def pinner(workflows: Workflows) -> Callable[..., Pinned]:
    """A workflow version as Runs starts it: the given version, the current one by default.
    Raises KeyError for an unknown workflow or version."""

    def pin(workflow_id: str, number: int | None = None) -> Pinned:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow not found: {workflow_id}")
        version = workflows.version(workflow_id, number or workflow.current_version)
        if version is None:
            raise KeyError(f"version not found: {workflow_id} v{number}")
        stages = [stage_def(s) for s in version.model.stages]
        return Pinned(workflow.world_id, workflow_id, version.number, workflow.name, stages)

    return pin


def act(action: Callable[[], Run]) -> Run:
    """The control layer's refusals as HTTP: unknown run 404, wrong moment 409, bad input 422."""
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, "run not found") from exc
    except Illegal as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def router(runner: Runner, workflows: Workflows, worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["runs"])
    pin = pinner(workflows)

    @api.post("/workflows/{workflow_id}/runs", status_code=201)
    def start_run(workflow_id: str, body: StartRun) -> Run:
        try:
            p = pin(workflow_id, body.version)
        except KeyError as exc:
            what = "version" if workflows.get(workflow_id) else "workflow"
            raise HTTPException(404, f"{what} not found") from exc
        return runner.start(p.world_id, p.workflow_id, p.version, p.name, p.stages)

    @api.get("/worlds/{world_id}/runs")
    def list_runs(world_id: str) -> list[Run]:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")
        return runner.list_all(world_id)

    @api.get("/runs/{run_id}")
    def get_run(run_id: str) -> Run:
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    @api.get("/gates")
    def list_gates() -> list[Gate]:
        """Every open gate across all worlds and runs, newest first: the inbox."""
        return runner.gates()

    @api.post("/runs/{run_id}/decision", responses=ACTION_ERRORS)
    def decide(run_id: str, body: Decide) -> Run:
        return act(lambda: runner.decide(run_id, body.choice, body.note, body.send_back_to))

    @api.post("/runs/{run_id}/pause", responses=ACTION_ERRORS)
    def pause(run_id: str) -> Run:
        """At a gate the pause is immediate; mid-stage it lands when the stage finishes."""
        return act(lambda: runner.pause(run_id))

    @api.post("/runs/{run_id}/resume", responses=ACTION_ERRORS)
    def resume(run_id: str) -> Run:
        return act(lambda: runner.resume(run_id))

    @api.post("/runs/{run_id}/abort", responses=ACTION_ERRORS)
    def abort(run_id: str) -> Run:
        """Aborts the run and every child run it started."""
        return act(lambda: runner.abort(run_id))

    return api
