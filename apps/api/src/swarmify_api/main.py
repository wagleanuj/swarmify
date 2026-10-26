"""FastAPI composition root. Serves the API under /api and the built web skin at /.

Run with `uvicorn --factory swarmify_api.main:create_app`; importing this module touches no disk.
`SWARMIFY_DATA_DIR` (default `~/.swarmify`) holds `swarmify.db`, the DBOS system database
`dbos.sqlite` and `worlds/<id>/`. DBOS launches in the lifespan, so runs execute only while the
app is served (or inside `with TestClient(app)`).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from importlib.metadata import version
from pathlib import Path

from anyio import to_thread
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from swarmify_api import runs, workflows, worlds
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_runs import MIGRATIONS as RUNS_MIGRATIONS
from swarmify_runs import Connector, Runner, StageDef
from swarmify_runs import SqliteStorage as RunStorage
from swarmify_workflows import MIGRATIONS as WORKFLOWS_MIGRATIONS
from swarmify_workflows import ConnectorRef, Workflows
from swarmify_workflows import SqliteStorage as WorkflowStorage
from swarmify_worlds import MIGRATIONS as WORLDS_MIGRATIONS
from swarmify_worlds import SqliteStorage, Worlds

VERSION = version("swarmify-api")
WEB_DIST = Path(os.environ.get("SWARMIFY_WEB_DIST", Path(__file__).parents[4] / "apps/web/dist"))


def data_dir() -> Path:
    return Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))


# ponytail: the built-in connectors every world gets (V1 spec, Connectors context), as a constant
# until T10 ships the connector registry, which replaces it with a per-world lookup.
BUILTIN_CONNECTORS = {
    c.name: c
    for c in [
        ConnectorRef("web_search", "tool", frozenset({"swarm"})),
        ConnectorRef("graph", "tool", frozenset({"swarm"})),
        ConnectorRef("remotion_render", "stage", frozenset({"connector"})),
        ConnectorRef("youtube", "publish", frozenset({"connector"})),
        ConnectorRef("upload_post", "publish", frozenset({"connector"})),
    ]
}


def migrated_db(root: Path) -> Path:
    db = root / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, WORLDS_MIGRATIONS, WORKFLOWS_MIGRATIONS, RUNS_MIGRATIONS)
    return db


def world_service(root: Path) -> Worlds:
    return Worlds(SqliteStorage(migrated_db(root)), root)


def workflow_service(root: Path) -> Workflows:
    return Workflows(WorkflowStorage(migrated_db(root)), lambda _: BUILTIN_CONNECTORS)


def noop_connector(stage_id: str, stage: StageDef) -> None:
    """ponytail: every connector stage is a no-op until T10 ships ConnectorTransport."""


def run_storage(root: Path) -> RunStorage:
    return RunStorage(migrated_db(root))


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


def create_app(root: Path | None = None, connector: Connector = noop_connector) -> FastAPI:
    root = root or data_dir()
    runner = Runner(run_storage(root), root / "dbos.sqlite", connector)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # DBOS refuses its sync API on a running event loop, so launch and stop in a thread.
        await to_thread.run_sync(runner.launch)
        yield
        await to_thread.run_sync(runner.shutdown)

    app = FastAPI(title="Swarmify", version=VERSION, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> Health:
        return Health(version=VERSION)

    world_svc, workflow_svc = world_service(root), workflow_service(root)

    def on_delete(world_id: str) -> None:
        workflow_svc.delete_world(world_id)
        runner.delete_world(world_id)

    app.include_router(worlds.router(world_svc, on_delete))
    app.include_router(workflows.router(workflow_svc, world_svc))
    app.include_router(runs.router(runner, workflow_svc, world_svc))
    if WEB_DIST.is_dir():
        app.mount("/", SpaFiles(directory=WEB_DIST, html=True), name="web")
    return app
