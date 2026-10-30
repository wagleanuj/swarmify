"""HTTP face of the Runs context: start a run of a workflow version or an Ad Hoc Run from a
typed goal, fork a run or promote a Library artifact into one, list runs, read one, act on one
(decide at its gate, pause, resume, abort, set its Budget), read the baseline it is measured
against, the world's spend this month, list every open gate and save a finished run as a
workflow."""

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from swarmify_api.workflows import INVALID, Report
from swarmify_runs import Gate, Illegal, Pinned, Run, RunControl, Runner, StageDef
from swarmify_swarm import OPERATOR, Task
from swarmify_workflows import Invalid, Stage, Workflows, WorkflowVersion, dump_workflow
from swarmify_worlds import MAX_AGENTS, Worlds

DONE_WHEN = "The goal is met and what was found is written down as notes and artifacts."
"""An Ad Hoc Run's done condition when none is typed."""
RETRO = StageDef(
    "retro",
    "retro",
    {
        "kind": "skill",
        "ask": "The retro read this run and proposes a change. Approve it, edit it, or reject it.",
    },
)
"""The stage every run ends with (ADR 0008): the Retro, whose proposal waits at a skill gate."""
ToolsFor = Callable[[str], list[str]]
"""(world id) to the names of the world's connectors a swarm stage may use."""
TasksOf = Callable[[str], list[Task]]
"""(stage id) to the tasks on the swarm stage's whiteboard."""


class StartRun(BaseModel):
    version: int | None = None
    """The workflow version to pin. The current one when left out."""
    budget_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    """The run's Budget, overriding the workflow's `budget_usd`; 5 USD when neither is set."""
    agents: int | None = Field(default=None, ge=1, le=MAX_AGENTS)
    """Workers per swarm stage, overriding the workflow's `agents` and the world's agent count."""
    baseline: bool = False
    """Baseline mode: every swarm stage runs one agent with no graph, to measure the swarm."""


class AdHoc(BaseModel):
    """An Ad Hoc Run: one swarm stage on a typed goal, using every connector of the world that
    swarm stages may use, with no saved workflow."""

    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    done_when: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""
    budget_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    baseline: bool = False


class Spend(BaseModel):
    """What the world's runs spent this calendar month against its Monthly Budget."""

    month: str
    spent_usd: float
    monthly_budget_usd: float | None


class SetBudget(BaseModel):
    """The run's new Budget in total, not an increment, so sending it twice adds nothing twice.
    Must be above what the run and its child runs have spent."""

    budget_usd: float = Field(gt=0, allow_inf_nan=False)


class Decide(BaseModel):
    """A gate answer. Pick gates need `choice` or `artifact`; approve gates may name
    `send_back_to`, an earlier stage that then runs again with `note` in its context; confirm
    gates take neither."""

    choice: str | None = Field(default=None, max_length=2_000)
    note: str = Field(default="", max_length=10_000)
    send_back_to: str | None = None
    artifact: str | None = Field(default=None, max_length=2_000)
    """The chosen artifact's path in the world folder, as `GET /runs/{run_id}/artifacts` lists
    it: the card picked, or what was approved. A pick with no `choice` takes its title."""
    edits: dict[str, Any] | None = None
    """At a gate before a publish stage: new values for fields its connector marks editable, by
    key. Every field's value as posted is recorded with the decision."""
    seen: dict[str, Any] | None = None
    """At a gate before a publish stage: the values the gate showed, by key. A field that is not
    editable and has changed since (a relative schedule now resolving to tomorrow) is refused
    with 422, so the inbox can show it again."""


class Fork(BaseModel):
    stage: int = Field(ge=0)
    """The first stage that runs anew; every stage before it is copied from the run."""


class Promote(BaseModel):
    artifact: str
    """The Library artifact's path in the world folder; it becomes the run's input."""
    stage: int = Field(ge=1)
    """The stage the run starts at; the earlier ones are skipped."""
    version: int | None = None


class SaveAsWorkflow(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    """The workflow's name; the run's name when left out."""


ACTION_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such run"},
    409: {"description": "The run is not in a state where this action applies"},
}


def stage_def(stage: Stage) -> StageDef:
    fields = asdict(stage)
    return StageDef(fields.pop("name"), fields.pop("type"), fields)


def with_agents(stages: list[StageDef], agents: int | None) -> list[StageDef]:
    """Each swarm stage carries the run's worker count in its config as `agents`, so a fork,
    retry or restart keeps it. None leaves the stages as they are (the world's agent count)."""
    if agents is None:
        return stages
    return [
        replace(s, config={**s.config, "agents": agents}) if s.type == "swarm" else s
        for s in stages
    ]


def pinner(workflows: Workflows) -> Callable[..., Pinned]:
    """A workflow version as Runs starts it: the given version, the current one by default.
    Every run start of a version comes through here (start, promote, fork, schedule, child run,
    MCP), so it runs today's workflow check first: rules and connectors change after a version
    is saved. Raises KeyError for an unknown workflow or version, Invalid when the version
    fails the check."""

    def pin(workflow_id: str, number: int | None = None) -> Pinned:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow not found: {workflow_id}")
        version = workflows.version(workflow_id, number or workflow.current_version)
        if version is None:
            raise KeyError(f"version not found: {workflow_id} v{number}")
        violations = workflows.check(workflow.world_id, version.yaml, workflow_id).violations
        if violations:
            raise Invalid(violations)
        stages = with_agents([stage_def(s) for s in version.model.stages], version.model.agents)
        return Pinned(
            workflow.world_id,
            workflow_id,
            version.number,
            workflow.name,
            stages,
            version.model.budget_usd,
            version.model.schedule,
        )

    return pin


def with_retro(stages: list[StageDef], baseline: bool = False) -> list[StageDef]:
    """The run's stages plus the Retro at the end. A baseline run has no judge to learn from."""
    return stages if baseline else [*stages, RETRO]


def with_hand_tasks(stage: StageDef, tasks: list[Task]) -> StageDef:
    """A swarm stage whose goal also names the tasks the operator added by hand, so the saved
    workflow's planner seeds them again. The agents' own tasks are theirs to plan anew."""
    lines = [f"- {t.title}: {t.body}" if t.body else f"- {t.title}" for t in tasks]
    if stage.type != "swarm" or not lines:
        return stage
    goal = f"{stage.config['goal']}\n\nAlso do these tasks:\n" + "\n".join(lines)
    return replace(stage, config={**stage.config, "goal": goal})


def start_pinned(
    control: RunControl,
    p: Pinned,
    retro: bool = True,
    budget_usd: float | None = None,
    agents: int | None = None,
    baseline: bool = False,
) -> Run:
    """Start a run of the pinned version as `POST /workflows/{id}/runs` does, for HTTP and MCP:
    `agents` workers per swarm stage, ending with the Retro when `retro` and not `baseline`."""
    stages = with_agents(p.stages, agents)
    return control.start(
        p.world_id,
        p.workflow_id,
        p.version,
        p.name,
        with_retro(stages, baseline) if retro else stages,
        budget_usd=budget_usd,
        workflow_budget_usd=p.budget_usd,
        baseline=baseline,
    )


def act(action: Callable[[], Run]) -> Run:
    """The control layer's refusals as HTTP: unknown run 404, wrong moment 409 (a spent Monthly
    Budget too), bad input 422."""
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, "run not found") from exc
    except Illegal as exc:
        raise HTTPException(409, str(exc)) from exc
    except Invalid:
        raise  # a run start's refusal, see `starting`
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def starting(start: Callable[[], Run]) -> Run | JSONResponse:
    """A run start as `act` answers it, plus a workflow version that fails today's check: 409
    with its violations, and a `detail` the skin shows as the reason."""
    try:
        return act(start)
    except Invalid as exc:
        detail = f"This version fails today's workflow check: {exc}"
        violations = jsonable_encoder(exc.violations)
        return JSONResponse(status_code=409, content={"detail": detail, "violations": violations})


def router(
    runner: Runner,
    workflows: Workflows,
    worlds: Worlds,
    tools_for: ToolsFor = lambda _world_id: [],
    retro: bool = True,
    tasks_of: TasksOf = lambda _stage_id: [],
) -> APIRouter:
    """`tools_for` names the world's connectors an Ad Hoc Run's stage gets as its tools. With
    `retro`, every run but a baseline ends with the Retro stage. `tasks_of` reads a swarm
    stage's whiteboard, whose hand-added tasks a run saved as a workflow keeps."""
    api = APIRouter(prefix="/api", tags=["runs"])
    pin = pinner(workflows)

    def stages(defs: list[StageDef], baseline: bool = False) -> list[StageDef]:
        return with_retro(defs, baseline) if retro else defs

    def pin_or_404(workflow_id: str, number: int | None) -> Pinned:
        try:
            return pin(workflow_id, number)
        except KeyError as exc:
            what = "version" if workflows.get(workflow_id) else "workflow"
            raise HTTPException(404, f"{what} not found") from exc

    @api.post(
        "/workflows/{workflow_id}/runs",
        status_code=201,
        responses={404: {}, 409: {}},
        response_model=Run,
    )
    def start_run(workflow_id: str, body: StartRun) -> Run | JSONResponse:
        """409 when the world's Monthly Budget is spent, or with the violations when the
        version fails today's workflow check."""
        return starting(
            lambda: start_pinned(
                runner,
                pin_or_404(workflow_id, body.version),
                retro,
                body.budget_usd,
                body.agents,
                body.baseline,
            )
        )

    @api.post("/worlds/{world_id}/runs", status_code=201, responses={404: {}, 409: {}})
    def start_ad_hoc(world_id: str, body: AdHoc) -> Run:
        """Start an Ad Hoc Run from a typed goal: one swarm stage with the world's swarm
        connectors as its tools, no workflow pinned. Its notes feed the world's graph like any
        run's. 409 when the world's Monthly Budget is spent."""
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")
        stage = StageDef(
            "goal",
            "swarm",
            {
                "goal": body.goal,
                "done_when": body.done_when or DONE_WHEN,
                "tools": tools_for(world_id),
            },
        )
        name = body.goal.splitlines()[0][:80]
        return act(
            lambda: runner.start(
                world_id,
                None,
                None,
                name,
                stages([stage], body.baseline),
                budget_usd=body.budget_usd,
                baseline=body.baseline,
            )
        )

    @api.get("/worlds/{world_id}/spend")
    def get_spend(world_id: str) -> Spend:
        """The world's estimated spend this month, what its Monthly Budget is measured against."""
        world = worlds.get(world_id)
        if world is None:
            raise HTTPException(404, "world not found")
        month = datetime.now(UTC).strftime("%Y-%m")
        return Spend(
            month=month,
            spent_usd=runner.storage.spent_in_month(world_id, month),
            monthly_budget_usd=world.monthly_budget_usd,
        )

    @api.post("/runs/{run_id}/fork", status_code=201, responses=ACTION_ERRORS, response_model=Run)
    def fork(run_id: str, body: Fork) -> Run | JSONResponse:
        """A new run of the same workflow version that copies this one, rows and files, up to
        `stage` and runs on from there. Every earlier stage must have finished. 409 with the
        violations when that version fails today's workflow check."""
        return starting(lambda: runner.fork(run_id, body.stage))

    @api.post(
        "/workflows/{workflow_id}/promote",
        status_code=201,
        responses={404: {}, 409: {}, 422: {}},
        response_model=Run,
    )
    def promote(workflow_id: str, body: Promote) -> Run | JSONResponse:
        """A run that starts at `stage` with `artifact` as its input; the earlier stages are
        skipped. 409 with the violations when the version fails today's workflow check."""

        def start() -> Run:
            p = pin_or_404(workflow_id, body.version)
            folder = worlds.folder(p.world_id)
            file = (folder / body.artifact).resolve()
            if not file.is_relative_to((folder / "runs").resolve()) or not file.is_file():
                raise HTTPException(404, "artifact not found")
            return runner.promote(
                replace(p, stages=stages(p.stages)), body.stage, file, body.artifact
            )

        return starting(start)

    @api.post(
        "/runs/{run_id}/workflow",
        status_code=201,
        responses={**ACTION_ERRORS, **INVALID},
        response_model=WorkflowVersion,
    )
    def save_as_workflow(run_id: str, body: SaveAsWorkflow) -> WorkflowVersion | JSONResponse:
        """A new workflow in the run's world from the stages the run actually ran (an ad hoc run
        becomes repeatable); each swarm stage's goal also names the tasks added to it by hand.
        Only a run that is done."""
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status != "done":
            raise HTTPException(409, f"the run is {run.status}; only a done run becomes a workflow")
        # The Retro is added to every run, never written in a workflow (with_retro).
        defs = [
            with_hand_tasks(
                runner.storage.stage_def(run.id, s.index)[1],
                [t for t in tasks_of(s.id) if t.added_by == OPERATOR],
            )
            for s in run.stages
        ]
        defs = [d for d in defs if d.type != RETRO.type]
        # The worker count each swarm stage carries (with_agents) is the workflow's `agents`.
        agents = next((d.config["agents"] for d in defs if d.config.get("agents")), None)
        yaml = dump_workflow(
            (body.name or run.name).strip() or run.name,
            [(d.type, d.name, {k: v for k, v in d.config.items() if k != "agents"}) for d in defs],
            budget_usd=run.budget_usd,
            agents=agents,
        )
        try:
            return workflows.save(run.world_id, yaml)
        except Invalid as exc:
            report = Report(violations=exc.violations, model=None)
            return JSONResponse(status_code=422, content=jsonable_encoder(report))

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

    @api.get("/runs/{run_id}/baseline")
    def get_baseline(run_id: str) -> Run | None:
        """The newest finished baseline run of the run's workflow, any version: its cost and
        duration are what the swarm is measured against. Null when there is none yet."""
        try:
            return runner.baseline(run_id)
        except KeyError as exc:
            raise HTTPException(404, "run not found") from exc

    @api.post("/runs/{run_id}/budget", responses=ACTION_ERRORS)
    def set_budget(run_id: str, body: SetBudget) -> Run:
        """Set the Budget of the run and its child runs. A run waiting at a budget gate goes on;
        422 when the new budget is not above what was spent."""
        return act(lambda: runner.set_budget(run_id, body.budget_usd))

    @api.get("/gates")
    def list_gates() -> list[Gate]:
        """Every open gate across all worlds and runs, newest first: the inbox."""
        return runner.gates()

    @api.post("/runs/{run_id}/decision", responses=ACTION_ERRORS)
    def decide(run_id: str, body: Decide) -> Run:
        return act(
            lambda: runner.decide(
                run_id,
                body.choice,
                body.note,
                body.send_back_to,
                artifact=body.artifact,
                edits=body.edits,
                seen=body.seen,
            )
        )

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
