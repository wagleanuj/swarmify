"""HTTP face of the Eval (Learning context): judge a task's two trials blind, store a finished
eval's trials and judgements as a report artifact in the eval world, and list the reports.
`swarmify-eval` (eval_harness) runs the trials."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarmify_kernel import new_id
from swarmify_learning.evals import (
    JUDGE_SYSTEM,
    KINDS,
    PICK_SCHEMA,
    TASKS,
    EvalReport,
    EvalTask,
    Judgement,
    Trial,
    judge_prompt,
    made_by,
    markdown,
    parse_pick,
    summarize,
)
from swarmify_memory import Memory
from swarmify_swarm import AgentRuntime, SessionInit, SessionSpec
from swarmify_worlds import World, Worlds

EVAL_WORLD = "Evals"
"""The world the reports land in, made on the first report. Trials each get a fresh world."""
DESCRIPTION = "Eval reports: the baseline against the swarm."
JUDGE_TURNS = 10
log = logging.getLogger(__name__)


class NewEval(BaseModel):
    k: int = Field(ge=1, le=20)
    kind: str | None = None
    """The task kind the trials ran, a key of KINDS."""
    trials: list[Trial] = Field(min_length=2, max_length=5_000)
    judgements: list[Judgement] = Field(default=[], max_length=2_500)


class Side(BaseModel):
    world_id: str
    run_id: str


class Pair(BaseModel):
    """One task's trial `index` on each arm, for the blind judge."""

    task_id: str
    index: int = Field(default=0, ge=0)
    baseline: Side
    swarm: Side


class _NoTools:
    """The judge reads only what its prompt shows."""

    def started(self, init: SessionInit) -> None: ...

    def before_tool(self, tool: str, input: dict[str, Any]) -> str | None:
        return "The judge decides from the prompt alone."

    def cost(self, usd: float) -> str | None:
        return None


def router(worlds: Worlds, memory: Memory, runtime: AgentRuntime) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["evals"])

    def eval_world() -> World | None:
        return next((w for w in worlds.list_all() if w.name == EVAL_WORLD), None)

    def outputs(task: EvalTask, side: Side) -> dict[str, str]:
        return {
            a.name: a.body
            for a in memory.library(side.world_id)
            if a.run_id == side.run_id and made_by(task, a.name)
        }

    @api.post("/evals/judge", responses={404: {}, 502: {}})
    async def judge(body: Pair) -> Judgement:
        """One blind judge session over what the task's workflow left on each side: the arm labels
        removed, the order shuffled per task. 502 when the session fails or answers in another
        shape; post again to retry."""
        task = next((t for t in TASKS if t.id == body.task_id), None)
        if task is None:
            raise HTTPException(404, "no such eval task")
        if worlds.get(body.baseline.world_id) is None or worlds.get(body.swarm.world_id) is None:
            raise HTTPException(404, "world not found")
        shown = {"baseline": outputs(task, body.baseline), "swarm": outputs(task, body.swarm)}
        world = eval_world() or worlds.create(EVAL_WORLD, DESCRIPTION)
        folder = worlds.folder(world.id)
        spec = SessionSpec(
            system_prompt=JUDGE_SYSTEM,
            prompt=judge_prompt(task, shown),
            cwd=folder,
            plugin=folder,
            tools=[],
            allowed=[],
            max_turns=JUDGE_TURNS,
            output_schema=PICK_SCHEMA,
        )
        try:
            end = await runtime.run(spec, _NoTools())
            if end.is_error:
                raise RuntimeError(end.result or "the session failed")
            return parse_pick(task.id, body.index, end.output)
        except Exception as exc:
            log.warning("eval judge failed on %s %d: %s", task.id, body.index, exc)
            raise HTTPException(502, f"The judge did not answer: {exc}") from exc

    @api.post("/evals", status_code=201, responses={422: {}})
    def save_eval(body: NewEval) -> EvalReport:
        """Summarize the trials and the judge's picks into a report (pass at k, pass to the k,
        cost, duration, acceptance, wins, losses, ties and cost a win per arm, and whether the
        kind is a swarm win) and store it as `report.md` in the eval world's Library."""
        now = datetime.now(UTC).isoformat()
        if body.kind is not None and body.kind not in KINDS:
            raise HTTPException(422, f"no such task kind {body.kind}")
        try:
            report = summarize(body.trials, body.k, new_id("eval"), now, body.judgements, body.kind)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        world = eval_world() or worlds.create(EVAL_WORLD, DESCRIPTION)
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
        world = eval_world()
        if world is None:
            return []
        library = memory.library(world.id)
        reports = [a.fields["report"] for a in library if a.type == "eval" and "report" in a.fields]
        return sorted(reports, key=lambda r: r["created_at"], reverse=True)

    return api
