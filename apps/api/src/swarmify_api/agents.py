"""HTTP face of a swarm stage's agents: who worked it, their sessions and estimated cost, the
stage's live log of every tool call the hooks saw, the judge's verdicts and the stage's
artifacts. Changes stream as `agent.updated`, `agent.logged`, `verdict.created` and
`artifact.created` events on the world's stream."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from swarmify_memory import ArtifactFile, Memory
from swarmify_runs import Run
from swarmify_swarm import Agent, LogEntry, Storage, Verdict


def router(
    board: Storage,
    memory: Memory,
    get_run: Callable[[str], Run | None],
    family: Callable[[str], list[str]],
) -> APIRouter:
    """`family` is the run and its child runs, which spend its budget (ADR 0013)."""
    api = APIRouter(prefix="/api", tags=["agents"])

    def stage_of(run_id: str, stage_id: str) -> Run:
        run = get_run(run_id)
        if run is None or not any(s.id == stage_id for s in run.stages):
            raise HTTPException(404, "stage not found")
        return run

    @api.get("/runs/{run_id}/agents")
    def get_agents(run_id: str) -> list[Agent]:
        """Every agent of the run and of its child runs, whose spend is the run's (ADR 0013);
        the run's estimated cost is the sum of theirs. Each agent names its own run."""
        if get_run(run_id) is None:
            raise HTTPException(404, "run not found")
        return [a for r in family(run_id) for a in board.list_agents(r)]

    @api.get("/runs/{run_id}/stages/{stage_id}/log")
    def get_log(run_id: str, stage_id: str) -> list[LogEntry]:
        stage_of(run_id, stage_id)
        return board.list_log(stage_id)

    @api.get("/runs/{run_id}/stages/{stage_id}/verdicts")
    def get_verdicts(run_id: str, stage_id: str) -> list[Verdict]:
        """The judge's verdict on every loop of the stage, oldest first."""
        stage_of(run_id, stage_id)
        return board.list_verdicts(stage_id)

    @api.get("/runs/{run_id}/stages/{stage_id}/artifacts")
    def get_artifacts(run_id: str, stage_id: str) -> list[ArtifactFile]:
        """The stage's artifact files as they are on disk: projected ones carry their type and
        fields (an idea's rank and source references) in `fields`."""
        run = stage_of(run_id, stage_id)
        return memory.stage_artifacts(run.world_id, run_id, stage_id)

    return api
