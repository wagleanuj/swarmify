"""HTTP face of a swarm stage's agents: who worked it, their sessions and estimated cost, and the
stage's live log of every tool call the hooks saw. Changes stream as `agent.updated` and
`agent.logged` events on the world's stream."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from swarmify_runs import Run
from swarmify_swarm import Agent, LogEntry, Storage


def router(
    board: Storage,
    get_run: Callable[[str], Run | None],
    family: Callable[[str], list[str]],
) -> APIRouter:
    """`family` is the run and its child runs, which spend its budget (ADR 0013)."""
    api = APIRouter(prefix="/api", tags=["agents"])

    @api.get("/runs/{run_id}/agents")
    def get_agents(run_id: str) -> list[Agent]:
        """Every agent of the run and of its child runs, whose spend is the run's (ADR 0013);
        the run's estimated cost is the sum of theirs. Each agent names its own run."""
        if get_run(run_id) is None:
            raise HTTPException(404, "run not found")
        return [a for r in family(run_id) for a in board.list_agents(r)]

    @api.get("/runs/{run_id}/stages/{stage_id}/log")
    def get_log(run_id: str, stage_id: str) -> list[LogEntry]:
        run = get_run(run_id)
        if run is None or not any(s.id == stage_id for s in run.stages):
            raise HTTPException(404, "stage not found")
        return board.list_log(stage_id)

    return api
