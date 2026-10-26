"""HTTP face of the Workflows context: save YAML as versions, check it, list and read versions."""

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from swarmify_workflows import (
    Invalid,
    Violation,
    Workflow,
    WorkflowModel,
    Workflows,
    WorkflowVersion,
)
from swarmify_worlds import Worlds


class YamlBody(BaseModel):
    yaml: str = Field(max_length=100_000)


class CheckBody(YamlBody):
    workflow_id: str | None = None


class Report(BaseModel):
    """What the editor shows inline: every violation, plus the model when the shape parsed."""

    violations: list[Violation]
    model: WorkflowModel | None = None


INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": Report, "description": "The workflow breaks one or more rules"}
}


def router(workflows: Workflows, worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["workflows"])

    def world_or_404(world_id: str) -> None:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")

    def workflow_or_404(workflow_id: str) -> Workflow:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise HTTPException(404, "workflow not found")
        return workflow

    def save(world_id: str, yaml: str, workflow_id: str | None) -> WorkflowVersion | JSONResponse:
        try:
            return workflows.save(world_id, yaml, workflow_id)
        except Invalid as exc:
            report = Report(violations=exc.violations, model=None)
            return JSONResponse(status_code=422, content=jsonable_encoder(report))

    @api.get("/worlds/{world_id}/workflows")
    def list_workflows(world_id: str) -> list[Workflow]:
        world_or_404(world_id)
        return workflows.list_all(world_id)

    @api.post(
        "/worlds/{world_id}/workflows",
        status_code=201,
        responses=INVALID,
        response_model=WorkflowVersion,
    )
    def create_workflow(world_id: str, body: YamlBody) -> WorkflowVersion | JSONResponse:
        world_or_404(world_id)
        return save(world_id, body.yaml, None)

    @api.post("/worlds/{world_id}/workflows/check")
    def check_workflow(world_id: str, body: CheckBody) -> Report:
        world_or_404(world_id)
        if body.workflow_id is not None:
            workflow_or_404(body.workflow_id)
        draft = workflows.check(world_id, body.yaml, body.workflow_id)
        return Report(violations=draft.violations, model=draft.model)

    @api.get("/workflows/{workflow_id}")
    def get_workflow(workflow_id: str) -> Workflow:
        return workflow_or_404(workflow_id)

    @api.get("/workflows/{workflow_id}/versions")
    def list_versions(workflow_id: str) -> list[WorkflowVersion]:
        workflow_or_404(workflow_id)
        return workflows.versions(workflow_id)

    @api.post(
        "/workflows/{workflow_id}/versions",
        status_code=201,
        responses=INVALID,
        response_model=WorkflowVersion,
    )
    def add_version(workflow_id: str, body: YamlBody) -> WorkflowVersion | JSONResponse:
        workflow = workflow_or_404(workflow_id)
        return save(workflow.world_id, body.yaml, workflow_id)

    @api.get("/workflows/{workflow_id}/versions/{number}")
    def get_version(workflow_id: str, number: int) -> WorkflowVersion:
        version = workflows.version(workflow_id, number)
        if version is None:
            raise HTTPException(404, "version not found")
        return version

    return api
