"""HTTP face of the Runs context: start a run of a workflow version, list runs, read one."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from swarmify_runs import Run, Runner, StageDef
from swarmify_workflows import Stage, Workflows
from swarmify_worlds import Worlds


class StartRun(BaseModel):
    version: int | None = None
    """The workflow version to pin. The current one when left out."""


def stage_def(stage: Stage) -> StageDef:
    fields = asdict(stage)
    return StageDef(fields.pop("name"), fields.pop("type"), fields)


def router(runner: Runner, workflows: Workflows, worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["runs"])

    @api.post("/workflows/{workflow_id}/runs", status_code=201)
    def start_run(workflow_id: str, body: StartRun) -> Run:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise HTTPException(404, "workflow not found")
        version = workflows.version(workflow_id, body.version or workflow.current_version)
        if version is None:
            raise HTTPException(404, "version not found")
        stages = [stage_def(s) for s in version.model.stages]
        return runner.start(workflow.world_id, workflow_id, version.number, workflow.name, stages)

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

    return api
