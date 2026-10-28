"""FastAPI composition root. Serves the API under /api and the built web skin at /.

Run with `uvicorn --factory swarmify_api.main:create_app`; importing this module touches no disk.
The GraphWriter runs in the API process unless `SWARMIFY_GRAPH_WRITER=external` says a
`swarmify-graph-writer` process does it instead; never both (ADR 0003).
`SWARMIFY_DATA_DIR` (default `~/.swarmify`) holds `swarmify.db`, the DBOS system database
`dbos.sqlite` and `worlds/<id>/`. DBOS launches in the lifespan, so runs execute only while the
app is served (or inside `with TestClient(app)`). `SWARMIFY_PASSWORD` turns on basic auth.
"""

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

import yaml
from anyio import to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from swarmify_api import (
    agents,
    connectors,
    evals,
    graph,
    learning,
    library,
    mcp,
    notifications,
    planner,
    runs,
    tasks,
    upkeep,
    workflows,
    worlds,
)
from swarmify_api.auth import BasicAuth
from swarmify_connectors import MIGRATIONS as CONNECTORS_MIGRATIONS
from swarmify_connectors import (
    ConnectorError,
    Connectors,
    ConnectorTransport,
    Credentials,
    McpTransport,
    publish,
)
from swarmify_connectors import SqliteStorage as ConnectorStorage
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_learning import MIGRATIONS as LEARNING_MIGRATIONS
from swarmify_learning import Learning
from swarmify_learning import SqliteStorage as LearningStorage
from swarmify_memory import (
    DEBOUNCE,
    GraphIndex,
    GraphWriter,
    McpGraphIndex,
    Memory,
    run_graphify,
)
from swarmify_notifications import MIGRATIONS as NOTIFICATIONS_MIGRATIONS
from swarmify_notifications import Notifications
from swarmify_notifications import SqliteStorage as ChannelStorage
from swarmify_runs import (
    DEFAULT_CAP,
    Connector,
    Notifier,
    OverBudget,
    Previews,
    PublishPreview,
    Run,
    RunControl,
    Runner,
    StageDef,
)
from swarmify_runs import MIGRATIONS as RUNS_MIGRATIONS
from swarmify_runs import SqliteStorage as RunStorage
from swarmify_swarm import MIGRATIONS as SWARM_MIGRATIONS
from swarmify_swarm import AgentRuntime, CastEntry, SdkRuntime, Stopped, Swarm
from swarmify_swarm import SqliteStorage as TaskStorage
from swarmify_swarm.sdk import PRICES, default_model
from swarmify_workflows import MIGRATIONS as WORKFLOWS_MIGRATIONS
from swarmify_workflows import ConnectorRef, Workflows, WorkflowVersion
from swarmify_workflows import SqliteStorage as WorkflowStorage
from swarmify_worlds import MIGRATIONS as WORLDS_MIGRATIONS
from swarmify_worlds import SqliteStorage, Worlds

log = logging.getLogger(__name__)
VERSION = version("swarmify-api")
WEB_DIST = Path(os.environ.get("SWARMIFY_WEB_DIST", Path(__file__).parents[4] / "apps/web/dist"))


def data_dir() -> Path:
    return Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))


def migrated_db(root: Path) -> Path:
    db = root / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(
            conn,
            KERNEL_MIGRATIONS,
            WORLDS_MIGRATIONS,
            WORKFLOWS_MIGRATIONS,
            RUNS_MIGRATIONS,
            CONNECTORS_MIGRATIONS,
            SWARM_MIGRATIONS,
            NOTIFICATIONS_MIGRATIONS,
            LEARNING_MIGRATIONS,
        )
    return db


def world_service(root: Path) -> Worlds:
    return Worlds(SqliteStorage(migrated_db(root)), root)


def connector_service(
    root: Path, transport: ConnectorTransport | None = None, poll_seconds: float = 2.0
) -> Connectors:
    """Credentials live in `<data_dir>/.env`, mode 600 (`~/.swarmify/.env` by default)."""
    return Connectors(
        ConnectorStorage(migrated_db(root)),
        transport or McpTransport(),
        Credentials(root / ".env"),
        poll_seconds,
    )


def workflow_service(
    root: Path, registry: Connectors | None = None, control: RunControl | None = None
) -> Workflows:
    """Workflows learns what a world's connectors allow from the registry, by name. With a
    `control`, every saved version's `schedule` becomes (or removes) a DBOS cron for it."""
    registry = registry or connector_service(root)

    def refs(world_id: str) -> dict[str, ConnectorRef]:
        def problems(config: dict[str, Any]) -> list[str]:
            return registry.stage_problems(world_id, config)

        return {
            c.name: ConnectorRef(c.name, c.kind, frozenset(c.stage_types), problems)
            for c in registry.list_all(world_id)
        }

    def on_saved(version: WorkflowVersion) -> None:
        if control is not None:
            control.schedule(version.workflow_id, version.model.schedule)

    return Workflows(WorkflowStorage(migrated_db(root)), refs, on_saved)


def unschedule_world(workflows: Workflows, control: RunControl, world_id: str) -> None:
    """Drop the DBOS schedules of a world's workflows; call before the workflows go."""
    for workflow in workflows.list_all(world_id):
        control.schedule(workflow.id, None)


class WorldRows(Protocol):
    def delete_world(self, world_id: str) -> None: ...


def world_deleter(
    worlds: Worlds, workflows: Workflows, control: RunControl, *contexts: WorldRows
) -> Callable[[str], bool]:
    """The one world delete, for HTTP and MCP alike: the world's row and folder, its
    schedules, its live runs (cancelled through DBOS, wherever they run), then every other
    context's rows. The delete returns False for an unknown world."""

    def delete(world_id: str) -> bool:
        if not worlds.delete(world_id):
            return False
        unschedule_world(workflows, control, world_id)  # before the workflows go
        workflows.delete_world(world_id)
        control.delete_world(world_id)
        for context in contexts:
            context.delete_world(world_id)
        return True

    return delete


def base_url() -> str:
    """Where deep links in Notifications point: the web skin as the operator reaches it."""
    return os.environ.get("SWARMIFY_BASE_URL", "http://localhost:8000")


def learning_service(root: Path) -> Learning:
    """Skills (shared ones in `<data_dir>/skills`, a world's own in its folder), retro
    proposals and the scorecard."""
    return Learning(LearningStorage(migrated_db(root)), root)


def notification_service(root: Path) -> Notifications:
    """The Notifier: each world's Slack Channel, set on its settings page."""
    return Notifications(ChannelStorage(migrated_db(root)), base_url())


def run_storage(root: Path) -> RunStorage:
    return RunStorage(migrated_db(root))


def task_board(root: Path) -> TaskStorage:
    return TaskStorage(migrated_db(root))


def utc_now() -> datetime:
    return datetime.now(UTC)


SWEEP_SECONDS = 15.0
"""How often silent claims are returned to open. Well under the two-minute heartbeat timeout."""


def frontmatter(text: str) -> dict[str, Any]:
    """The YAML between a markdown file's leading `---` fences; {} when there is none."""
    if not text.startswith("---\n"):
        return {}
    try:
        front = yaml.safe_load(text[4:].split("\n---", 1)[0])
    except yaml.YAMLError:
        return {}
    return front if isinstance(front, dict) else {}


def publish_config(
    world_folder: Path, run: Run, stage: StageDef, edits: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """A publish stage's config as it goes out, and the problem finding its metadata, if any.
    `params.metadata` names a file an earlier stage of the run left (like script.md) whose
    frontmatter `title` and `description` win over the params; the edits made at the gate win
    over both."""
    params = dict(stage.config.get("params") or {})
    name, problem = params.pop("metadata", None), None
    if name:
        try:
            front = frontmatter(run_file(world_folder, run, str(name)).read_text(errors="replace"))
            params.update({k: str(front[k]) for k in ("title", "description") if front.get(k)})
        except ConnectorError as exc:
            problem = f"params.metadata: {exc}"
    return {**stage.config, "params": {**params, **edits}}, problem


def previews(registry: Connectors, worlds: Worlds) -> Previews:
    """Runs learns what a stage will publish from the registry, which knows connector kinds."""

    def preview(run: Run, stage: StageDef, edits: dict[str, Any]) -> PublishPreview | None:
        if registry.publish_preview(run.world_id, stage.config) is None:
            return None
        config, problem = publish_config(worlds.folder(run.world_id), run, stage, edits)
        shown = registry.publish_preview(run.world_id, config)
        assert shown is not None  # the same connector as above
        if problem:
            shown["problems"].append(problem)
        return PublishPreview(**shown)

    return preview


def run_control(root: Path, notifier: Notifier | None = None) -> RunControl:
    """Act on runs from a process that does not host DBOS, such as the MCP server."""
    return RunControl(
        run_storage(root),
        root / "dbos.sqlite",
        root,
        notifier or notification_service(root),
        previews(connector_service(root), world_service(root)),
    )


def run_file(world_folder: Path, run: Run, name: str) -> Path:
    """The newest file called `name` any stage of the run left, for a stage's `input`."""
    if Path(name).name != name or name in ("", ".", ".."):
        raise ConnectorError(f"input must be a bare file name, got '{name}'")
    found = [p for p in (world_folder / "runs" / run.id).rglob(name) if p.is_file()]
    if not found:
        raise ConnectorError(f"no earlier stage of this run left a file named '{name}'")
    return max(found, key=lambda p: p.stat().st_mtime)


def with_files(world_folder: Path, run: Run, stage_id: str, config: dict[str, Any]) -> dict:
    """A connector stage exchanges files through the run folder. `output` lands in this stage's
    `artifacts/` (created for it), `input` is the newest run file of that name (`run_file`), and
    the frontmatter a sidecar needs rides along. All join the call's params. Workflow YAML is
    Planner-authored, so a path in `output` or a run-owned key in `params` is refused."""
    params = dict(config.get("params") or {})
    for key in ("input_path", "output_path", "frontmatter"):
        if key in params:
            raise ConnectorError(f"params.{key} is set by the run, not the workflow")
    output = str(config.get("output") or "")
    if output and (Path(output).name != output or output in (".", "..")):
        raise ConnectorError(f"output must be a bare file name, got '{output}'")
    if config.get("input"):
        params["input_path"] = str(run_file(world_folder, run, str(config["input"])))
    if output:
        out = world_folder / "runs" / run.id / stage_id / "artifacts" / output
        out.parent.mkdir(parents=True, exist_ok=True)
        params["output_path"] = str(out)
    params["frontmatter"] = {"world": run.world_id, "run": run.id, "stage": stage_id}
    return {**config, "params": params}


class SpaFiles(StaticFiles):
    """The built skin, answering unknown non-API paths with index.html so deep links load."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path == "api" or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


class Health(BaseModel):
    version: str


class Models(BaseModel):
    default: str
    models: list[str]


def create_app(
    root: Path | None = None,
    connector: Connector | None = None,
    transport: ConnectorTransport | None = None,
    poll_seconds: float = 2.0,
    clock: Callable[[], datetime] = utc_now,
    sweep_seconds: float = SWEEP_SECONDS,
    runtime: AgentRuntime | None = None,
    graph_index: GraphIndex | None = None,
    extract: Callable[[Path], None] = run_graphify,
    graph_debounce: float = DEBOUNCE,
    graph_poll: float = 2.0,
    notifier: Notifier | None = None,
    upkeep_seconds: float = upkeep.UPKEEP_SECONDS,
    retro: bool = True,
) -> FastAPI:
    """`transport` swaps the MCP transport (tests pass the fake), `poll_seconds` is the wait
    between long-job polls, and `connector` replaces the connector stage call altogether.
    `clock` stamps claims and heartbeats and decides when a claim has gone silent. `runtime`
    runs agent sessions: the Agent SDK by default, a scripted transport in tests.
    `notifier` receives `gate.opened`, `budget.warning` and `run.ended`; each world's Slack
    Channel by default. Every `upkeep_seconds` the view counts are written back and old
    binaries pruned. `retro` ends every run but a baseline with the Retro stage (ADR 0008); tests
    that script no retro session turn it off."""
    root = root or data_dir()
    registry = connector_service(root, transport, poll_seconds)
    whiteboard = task_board(root)
    world_svc = world_service(root)
    memory = Memory(migrated_db(root), root)
    channels = notification_service(root)
    learning_svc = learning_service(root)

    def call_connector(run: Run, stage_id: str, stage: StageDef) -> None:
        shown = registry.publish_preview(run.world_id, stage.config)
        if shown is None:
            folder = world_svc.folder(run.world_id)
            config = with_files(folder, run, stage_id, stage.config)
            registry.run_stage(run.world_id, run.id, stage_id, config)
            output = Path(config["params"].get("output_path") or "")
            if output.is_file():  # announce what the connector left as an artifact
                memory.record(run.world_id, run.id, stage_id, str(stage.config["calls"]), output)
            return
        # A publish stage posts the input file with what was confirmed at the gate before it,
        # then records what went out as an artifact.
        index = next(s.index for s in run.stages if s.id == stage_id)
        confirmed = next(
            (d.publish for d in reversed(run.decisions) if d.stage_index == index - 1), None
        )
        folder = world_svc.folder(run.world_id)
        config, problem = publish_config(folder, run, stage, confirmed or {})
        if problem:
            raise ConnectorError(problem)
        shown = registry.publish_preview(run.world_id, config) or shown
        if not shown["problems"]:  # else run_stage refuses, naming them
            name = str(stage.config.get("input") or "")
            config["params"]["input_path"] = str(run_file(folder, run, name))
        call = registry.run_stage(run.world_id, run.id, stage_id, config)
        out = registry.publish_result(call)
        keys = ("connector", "title", "description", "visibility", "schedule", "made_for_kids")
        fields = {k: shown[k] for k in keys}
        memory.write_artifact(
            run.world_id,
            run.id,
            stage_id,
            "publish",
            "publish.md",
            publish.markdown(shown, out),
            kind="publish",
            fields={**fields, "status": out["status"], "links": out["links"], "id": out["id"]},
        )

    worlds_dir = root / "worlds"
    worlds_dir.mkdir(parents=True, exist_ok=True)
    index = graph_index or McpGraphIndex(worlds_dir)
    writer = GraphWriter(migrated_db(root), worlds_dir, extract, graph_debounce)
    memory = Memory(migrated_db(root), root)
    swarm = Swarm(
        whiteboard,
        runtime or SdkRuntime(),
        memory,
        graphs=graph.WorldGraphs(world_svc, index),
        servers=registry.agent_servers,
        clock=clock,
        budget=lambda run_id: runner.check_stop(run_id),  # the Budget, pause and abort live in Runs
    )

    def run_swarm(run: Run, stage_id: str, stage: StageDef) -> None:
        folder = world_svc.folder(run.world_id)
        learning_svc.sync(run.world_id)  # shared skills the world lacks load from its folder
        notes = next(s.notes for s in run.stages if s.id == stage_id)
        # The Cast as it stands when the stage starts: an edit reaches every later stage.
        members = world_svc.cast(run.world_id) or []
        world = world_svc.get(run.world_id)
        staged = stage.config.get("model")  # the stage's model wins over the world's choice

        def model(role: str) -> str | None:
            return staged or (world.model_for(role) if world else None)

        cast = [CastEntry(m.name, m.role, m.prompt, m.tools, model(m.role)) for m in members]  # type: ignore[arg-type]
        try:
            swarm.run_stage(
                run.world_id,
                run.id,
                stage_id,
                stage.name,
                stage.config,
                folder,
                notes,
                run.send_backs,
                baseline=run.baseline,
                cast=cast,
            )
        except Stopped as exc:
            raise OverBudget(str(exc)) from exc

    def run_retro(run: Run, stage_id: str, stage: StageDef) -> bool:
        """The Retro stage: a judge-style session over the world's events, through the swarm's
        session seam so it is logged, costed and budgeted like any agent. True when it recorded
        a proposal, which then waits at the stage's skill gate."""
        folder = world_svc.folder(run.world_id)
        yaml = None
        if run.workflow_id and run.workflow_version:
            version = workflow_svc.version(run.workflow_id, run.workflow_version)
            yaml = version.yaml if version else None

        world = world_svc.get(run.world_id)
        model = world.model_for("retro") if world else None

        def ask(system: str, prompt: str, schema: dict[str, Any], parse: Any) -> Any:
            try:
                return swarm.ask(
                    run.world_id, run.id, stage_id, stage.name, folder, "retro",
                    system, prompt, schema, parse, model,
                )  # fmt: skip
            except Stopped as exc:
                raise OverBudget(str(exc)) from exc

        learning_svc.sync(run.world_id)
        events = world_svc.events_after(run.world_id)
        made = learning_svc.retro(
            run.world_id, run.id, stage_id, run.workflow_id, yaml, events, ask
        )
        return made is not None

    def spend(run_ids: list[str]) -> float:
        """The agents' estimated cost of these runs: what their Budget counts."""
        return sum(a.cost_usd for r in run_ids for a in whiteboard.list_agents(r))

    def take(child: Run, run: Run, stage_id: str, names: list[str]) -> None:
        stage = next(s.name for s in run.stages if s.id == stage_id)
        source = (child.world_id, child.id, [s.id for s in child.stages])
        memory.copy_artifacts(source, names, run.world_id, run.id, stage_id, f"workflow:{stage}")

    def setting(world_id: str, name: str) -> Any:
        world = world_svc.get(world_id)
        return getattr(world, name) if world else None

    runner = Runner(
        run_storage(root),
        root / "dbos.sqlite",
        connector or call_connector,
        root,
        notifier or channels,
        swarm=run_swarm,
        take=take,
        previews=previews(registry, world_svc),
        spend=spend,
        monthly=lambda w: setting(w, "monthly_budget_usd"),
        cap_for=lambda w: setting(w, "run_cap") or DEFAULT_CAP,
        retro=run_retro,
    )
    workflow_svc = workflow_service(root, registry, runner)
    runner.pin = runs.pinner(workflow_svc)

    def swarm_tools(world_id: str) -> list[str]:
        """The world's connectors a swarm stage may use: an Ad Hoc Run's tools."""
        return [c.name for c in registry.list_all(world_id) if "swarm" in c.stage_types]

    async def sweep() -> None:
        while True:
            try:
                await to_thread.run_sync(lambda: whiteboard.expire(clock()))
            except Exception:
                log.exception("whiteboard sweep failed, retrying next tick")
            await asyncio.sleep(sweep_seconds)

    def upkeep_once() -> None:
        upkeep.record_outcomes(world_svc, memory, registry, clock())
        upkeep.prune(world_svc, memory, clock())

    async def upkeep_loop() -> None:
        # ponytail: an in-process interval, restarted with the app; both jobs are idempotent, so
        # a missed tick only delays them. Move to DBOS cron if they must run at fixed times.
        # Runs once at launch too, so an app restarted more often than the interval still does it.
        while True:
            try:
                await to_thread.run_sync(upkeep_once)
            except Exception:
                log.exception("upkeep failed, retrying next tick")
            await asyncio.sleep(upkeep_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # DBOS refuses its sync API on a running event loop, so launch and stop in a thread.
        await to_thread.run_sync(runner.launch)
        # The workflows table decides what is scheduled; DBOS's schedules follow it.
        await to_thread.run_sync(lambda: runner.sync_schedules(workflow_svc.scheduled()))
        sweeper = asyncio.create_task(sweep())
        upkeeper = asyncio.create_task(upkeep_loop())
        stop = threading.Event()
        # The GraphWriter's one thread: every world's rebuilds in turn (ADR 0003).
        rebuilds = threading.Thread(
            target=writer.run, args=(stop, graph_poll), name="graph-writer", daemon=True
        )
        if os.environ.get("SWARMIFY_GRAPH_WRITER") != "external":
            rebuilds.start()
        yield
        stop.set()
        for task in (sweeper, upkeeper):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if rebuilds.is_alive():
            await to_thread.run_sync(lambda: rebuilds.join(5))
        await to_thread.run_sync(index.close)
        await to_thread.run_sync(runner.shutdown)

    app = FastAPI(title="Swarmify", version=VERSION, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> Health:
        return Health(version=VERSION)

    @app.get("/api/models")
    def models() -> Models:
        """The models a world, a role or a stage may choose, and the one used when none does
        (`SWARMIFY_MODEL`, else Claude Opus 5.5). Any other model id is priced at the most
        expensive of these."""
        return Models(default=default_model(), models=list(PRICES))

    delete = world_deleter(
        world_svc, workflow_svc, runner, registry, whiteboard, channels, learning_svc
    )
    app.include_router(worlds.router(world_svc, delete, lambda w: runner.set_cap(w.id)))
    app.include_router(worlds.events_router(world_svc))
    app.include_router(workflows.router(workflow_svc, world_svc))
    app.include_router(runs.router(runner, workflow_svc, world_svc, swarm_tools, retro))
    app.include_router(connectors.router(registry, world_svc))
    app.include_router(tasks.router(whiteboard, runner.get, clock))
    app.include_router(agents.router(whiteboard, memory, runner.get, runner.family))
    app.include_router(graph.router(world_svc, index))
    app.include_router(library.router(memory, world_svc, runner.list_all))
    app.include_router(notifications.router(channels, world_svc))
    app.include_router(
        learning.router(learning_svc, world_svc, workflow_svc, runner, world_svc.events_after)
    )
    app.include_router(evals.router(world_svc, memory, swarm.runtime))
    tools = mcp.server(world_svc, workflow_svc, whiteboard, runner, delete)
    app.include_router(planner.router(world_svc, swarm.runtime, tools))
    if WEB_DIST.is_dir():
        app.mount("/", SpaFiles(directory=WEB_DIST, html=True), name="web")
    if password := os.environ.get("SWARMIFY_PASSWORD"):
        user = os.environ.get("SWARMIFY_USER", "swarmify")
        app.add_middleware(BasicAuth, user=user, password=password)
    return app
