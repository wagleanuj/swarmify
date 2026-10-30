"""HTTP face of the Workflows context: save YAML as versions, check it, list and read versions,
copy a workflow to another world."""

from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from swarmify_workflows import (
    STARTERS,
    Invalid,
    Starter,
    Violation,
    Workflow,
    WorkflowModel,
    Workflows,
    WorkflowVersion,
    cron_problem,
    next_fire,
)
from swarmify_worlds import Worlds


class YamlBody(BaseModel):
    yaml: str = Field(max_length=100_000)


class CheckBody(YamlBody):
    workflow_id: str | None = None


class CopyBody(BaseModel):
    world_id: str


class Report(BaseModel):
    """What the editor shows inline: every violation, plus the model when the shape parsed and,
    when it has a schedule, the next time DBOS would fire it."""

    violations: list[Violation]
    model: WorkflowModel | None = None
    next_run_at: str | None = None


def next_run(model: WorkflowModel | None) -> str | None:
    if model is None or not model.schedule or cron_problem(model.schedule):
        return None
    return next_fire(model.schedule).isoformat()


@dataclass(frozen=True)
class ListedWorkflow(Workflow):
    """A workflow as its world lists it, with what its current version breaks by today's check:
    a version saved before a rule or with a connector since removed is refused at run start."""

    violations: list[Violation] = field(default_factory=list)


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

    @api.get("/workflows/starters")
    def list_starters() -> list[Starter]:
        """The starter workflows a new workflow can begin from, each passing the check."""
        return STARTERS

    @api.get("/worlds/{world_id}/workflows")
    def list_workflows(world_id: str) -> list[ListedWorkflow]:
        world_or_404(world_id)
        listed = []
        for w in workflows.list_all(world_id):
            current = workflows.version(w.id, w.current_version)
            draft = workflows.check(w.world_id, current.yaml if current else "", w.id)
            listed.append(ListedWorkflow(**asdict(w), violations=draft.violations))
        return listed

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
        return Report(
            violations=draft.violations, model=draft.model, next_run_at=next_run(draft.model)
        )

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

    @api.post(
        "/workflows/{workflow_id}/copy",
        status_code=201,
        responses=INVALID,
        response_model=WorkflowVersion,
    )
    def copy_workflow(workflow_id: str, body: CopyBody) -> WorkflowVersion | JSONResponse:
        """A new workflow in another world from this one's current version. Refused with the
        violations when that world lacks a connector the workflow needs."""
        workflow_or_404(workflow_id)
        world_or_404(body.world_id)
        try:
            return workflows.copy(workflow_id, body.world_id)
        except Invalid as exc:
            report = Report(violations=exc.violations, model=None)
            return JSONResponse(status_code=422, content=jsonable_encoder(report))

    @api.get("/workflows/{workflow_id}/versions/{number}")
    def get_version(workflow_id: str, number: int) -> WorkflowVersion:
        version = workflows.version(workflow_id, number)
        if version is None:
            raise HTTPException(404, "version not found")
        return version

    return api
