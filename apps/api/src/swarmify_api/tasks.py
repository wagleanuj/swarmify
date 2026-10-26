"""HTTP face of the Swarm whiteboard: tasks of a swarm stage, added, claimed, heartbeated, done.

Swarm cannot import Runs, so this composition-root module resolves a run's stage first and hands
the Whiteboard its world, run and stage ids.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StringConstraints

from swarmify_runs import Run
from swarmify_swarm import OPERATOR, Refused, Storage, Task

GetRun = Callable[[str], Run | None]
ENDED = ("done", "failed", "skipped")


class NewTask(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    body: Annotated[str, StringConstraints(max_length=10_000)] = ""
    depends_on: list[str] = []
    """Tasks of the same stage that must be done first."""
    added_by: Annotated[str, StringConstraints(min_length=1, max_length=80)] = OPERATOR


class Claim(BaseModel):
    agent: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class Progress(Claim):
    note: Annotated[str, StringConstraints(max_length=10_000)] | None = None
    """Partial notes, kept on the task even if the claim later times out."""


def add_task(
    board: Storage,
    get_run: GetRun,
    run_id: str,
    stage_id: str,
    title: str,
    *,
    body: str = "",
    depends_on: tuple[str, ...] = (),
    added_by: str = OPERATOR,
) -> Task:
    """Add a task to a swarm stage that has not ended. KeyError for an unknown run or stage."""
    run = _stage_run(get_run, run_id, stage_id)
    stage = next(s for s in run.stages if s.id == stage_id)
    if stage.type != "swarm":
        raise Refused(f"{stage.name} is a {stage.type} stage; only swarm stages have a whiteboard")
    if stage.status in ENDED:
        raise Refused(f"{stage.name} has ended ({stage.status}); its whiteboard is read-only")
    return board.add_task(
        run.world_id, run.id, stage.id, title, added_by=added_by, body=body, depends_on=depends_on
    )


def list_tasks(board: Storage, get_run: GetRun, run_id: str, stage_id: str) -> list[Task]:
    _stage_run(get_run, run_id, stage_id)
    return board.list_tasks(stage_id)


def _stage_run(get_run: GetRun, run_id: str, stage_id: str) -> Run:
    run = get_run(run_id)
    if run is None or not any(s.id == stage_id for s in run.stages):
        raise KeyError("stage not found")
    return run


def router(board: Storage, get_run: GetRun, clock: Callable[[], datetime]) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["tasks"])

    def act[T](fn: Callable[[], T], missing: str = "task not found") -> T:
        try:
            return fn()
        except KeyError as exc:
            raise HTTPException(404, missing) from exc
        except Refused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/runs/{run_id}/stages/{stage_id}/tasks")
    def get_tasks(run_id: str, stage_id: str) -> list[Task]:
        return act(lambda: list_tasks(board, get_run, run_id, stage_id), "stage not found")

    @api.post("/runs/{run_id}/stages/{stage_id}/tasks", status_code=201)
    def post_task(run_id: str, stage_id: str, body: NewTask) -> Task:
        return act(
            lambda: add_task(
                board,
                get_run,
                run_id,
                stage_id,
                body.title,
                body=body.body,
                depends_on=tuple(body.depends_on),
                added_by=body.added_by,
            ),
            "stage not found",
        )

    @api.get("/tasks/{task_id}")
    def get_task(task_id: str) -> Task:
        task = board.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task

    @api.post("/tasks/{task_id}/claim")
    def claim(task_id: str, body: Claim) -> Task:
        """Atomic: of any number of concurrent claims, exactly one wins; the rest get a 409."""
        return act(lambda: board.claim(task_id, body.agent, clock()))

    @api.post("/tasks/{task_id}/heartbeat")
    def heartbeat(task_id: str, body: Progress) -> Task:
        return act(lambda: board.heartbeat(task_id, body.agent, body.note, clock()))

    @api.post("/tasks/{task_id}/finish")
    def finish(task_id: str, body: Progress) -> Task:
        return act(lambda: board.finish(task_id, body.agent, body.note, clock()))

    return api
