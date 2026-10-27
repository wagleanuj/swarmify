"""HTTP face of the Eval (Learning context): store a finished eval's trials as a report artifact
in the eval world, and list the reports. `swarmify-eval` (eval_harness) runs the trials."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarmify_kernel import new_id
from swarmify_learning.evals import EvalReport, Trial, markdown, summarize
from swarmify_memory import Memory
from swarmify_worlds import World, Worlds

EVAL_WORLD = "Evals"
"""The world the reports land in, made on the first report. Trials each get a fresh world."""


class NewEval(BaseModel):
    k: int = Field(ge=1, le=20)
    trials: list[Trial] = Field(min_length=2, max_length=5_000)


def router(worlds: Worlds, memory: Memory) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["evals"])

    def eval_world(create: bool) -> World | None:
        found = next((w for w in worlds.list_all() if w.name == EVAL_WORLD), None)
        if found or not create:
            return found
        return worlds.create(EVAL_WORLD, "Eval reports: the baseline against the swarm.")

    @api.post("/evals", status_code=201, responses={422: {}})
    def save_eval(body: NewEval) -> EvalReport:
        """Summarize the trials into a report (pass at k, pass to the k, cost, duration and
        acceptance per arm) and store it as `report.md` in the eval world's Library."""
        now = datetime.now(UTC).isoformat()
        try:
            report = summarize(body.trials, body.k, new_id("eval"), now)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        world = eval_world(create=True)
        assert world is not None
        memory.write_artifact(
            world.id,
            report.id,
            "report",
            "eval",
            "report.md",
            markdown(report),
            kind="eval",
            fields={"title": report.verdict, "report": asdict(report)},
        )
        return report

    @api.get("/evals", response_model=list[EvalReport])
    def list_evals() -> list[dict[str, Any]]:
        """Every stored report, newest first."""
        world = eval_world(create=False)
        if world is None:
            return []
        reports = [a.fields["report"] for a in memory.library(world.id) if a.type == "eval"]
        return sorted(reports, key=lambda r: r["created_at"], reverse=True)

    return api
