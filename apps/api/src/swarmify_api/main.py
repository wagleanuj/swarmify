"""FastAPI composition root. Serves the API under /api and the built web skin at /.

Run with `uvicorn --factory swarmify_api.main:create_app`; importing this module touches no disk.
`SWARMIFY_DATA_DIR` (default `~/.swarmify`) holds `swarmify.db`, the DBOS system database
`dbos.sqlite` and `worlds/<id>/`. DBOS launches in the lifespan, so runs execute only while the
app is served (or inside `with TestClient(app)`).
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from anyio import to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from swarmify_api import agents, connectors, runs, tasks, workflows, worlds
from swarmify_connectors import MIGRATIONS as CONNECTORS_MIGRATIONS
from swarmify_connectors import Connectors, ConnectorTransport, Credentials, McpTransport
from swarmify_connectors import SqliteStorage as ConnectorStorage
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import Memory
from swarmify_runs import MIGRATIONS as RUNS_MIGRATIONS
from swarmify_runs import Connector, Run, Runner, StageDef
from swarmify_runs import SqliteStorage as RunStorage
from swarmify_swarm import MIGRATIONS as SWARM_MIGRATIONS
from swarmify_swarm import AgentRuntime, SdkRuntime, Swarm
from swarmify_swarm import SqliteStorage as TaskStorage
from swarmify_workflows import MIGRATIONS as WORKFLOWS_MIGRATIONS
from swarmify_workflows import ConnectorRef, Workflows
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


def workflow_service(root: Path, registry: Connectors | None = None) -> Workflows:
    """Workflows learns what a world's connectors allow from the registry, by name."""
    registry = registry or connector_service(root)

    def refs(world_id: str) -> dict[str, ConnectorRef]:
        return {
            c.name: ConnectorRef(c.name, c.kind, frozenset(c.stage_types))
            for c in registry.list_all(world_id)
        }

    return Workflows(WorkflowStorage(migrated_db(root)), refs)


def run_storage(root: Path) -> RunStorage:
    return RunStorage(migrated_db(root))


def task_board(root: Path) -> TaskStorage:
    return TaskStorage(migrated_db(root))


def utc_now() -> datetime:
    return datetime.now(UTC)


SWEEP_SECONDS = 15.0
"""How often silent claims are returned to open. Well under the two-minute heartbeat timeout."""


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


def create_app(
    root: Path | None = None,
    connector: Connector | None = None,
    transport: ConnectorTransport | None = None,
    poll_seconds: float = 2.0,
    clock: Callable[[], datetime] = utc_now,
    sweep_seconds: float = SWEEP_SECONDS,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    """`transport` swaps the MCP transport (tests pass the fake), `poll_seconds` is the wait
    between long-job polls, and `connector` replaces the connector stage call altogether.
    `clock` stamps claims and heartbeats and decides when a claim has gone silent. `runtime`
    runs agent sessions: the Agent SDK by default, a scripted transport in tests."""
    root = root or data_dir()
    registry = connector_service(root, transport, poll_seconds)

    def call_connector(run: Run, stage_id: str, stage: StageDef) -> None:
        registry.run_stage(run.world_id, run.id, stage_id, stage.config)

    whiteboard = task_board(root)
    world_svc = world_service(root)
    swarm = Swarm(
        whiteboard,
        runtime or SdkRuntime(),
        Memory(migrated_db(root), root),
        servers=registry.agent_servers,
        clock=clock,
    )

    def run_swarm(run: Run, stage_id: str, stage: StageDef) -> None:
        folder = world_svc.folder(run.world_id)
        swarm.run_stage(run.world_id, run.id, stage_id, stage.name, stage.config, folder)

    runner = Runner(
        run_storage(root), root / "dbos.sqlite", connector or call_connector, swarm=run_swarm
    )

    async def sweep() -> None:
        while True:
            try:
                await to_thread.run_sync(lambda: whiteboard.expire(clock()))
            except Exception:
                log.exception("whiteboard sweep failed, retrying next tick")
            await asyncio.sleep(sweep_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # DBOS refuses its sync API on a running event loop, so launch and stop in a thread.
        await to_thread.run_sync(runner.launch)
        sweeper = asyncio.create_task(sweep())
        yield
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        await to_thread.run_sync(runner.shutdown)

    app = FastAPI(title="Swarmify", version=VERSION, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> Health:
        return Health(version=VERSION)

    workflow_svc = workflow_service(root, registry)

    def on_delete(world_id: str) -> None:
        workflow_svc.delete_world(world_id)
        runner.delete_world(world_id)
        registry.delete_world(world_id)
        whiteboard.delete_world(world_id)

    app.include_router(worlds.router(world_svc, on_delete))
    app.include_router(workflows.router(workflow_svc, world_svc))
    app.include_router(runs.router(runner, workflow_svc, world_svc))
    app.include_router(connectors.router(registry, world_svc))
    app.include_router(tasks.router(whiteboard, runner.get, clock))
    app.include_router(agents.router(whiteboard, runner.get))
    if WEB_DIST.is_dir():
        app.mount("/", SpaFiles(directory=WEB_DIST, html=True), name="web")
    return app
