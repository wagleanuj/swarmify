"""FastAPI composition root. Serves the API under /api and the built web skin at /.

Run with `uvicorn --factory swarmify_api.main:create_app`; importing this module touches no disk.
`SWARMIFY_DATA_DIR` (default `~/.swarmify`) holds `swarmify.db` and `worlds/<id>/`.
"""

import os
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from swarmify_api import worlds
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_worlds import MIGRATIONS as WORLDS_MIGRATIONS
from swarmify_worlds import SqliteStorage, Worlds

VERSION = version("swarmify-api")
WEB_DIST = Path(os.environ.get("SWARMIFY_WEB_DIST", Path(__file__).parents[4] / "apps/web/dist"))


def data_dir() -> Path:
    return Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))


def world_service(root: Path) -> Worlds:
    db = root / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, WORLDS_MIGRATIONS)
    return Worlds(SqliteStorage(db), root)


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


def create_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(title="Swarmify", version=VERSION)

    @app.get("/api/health")
    def health() -> Health:
        return Health(version=VERSION)

    app.include_router(worlds.router(world_service(root or data_dir())))
    if WEB_DIST.is_dir():
        app.mount("/", SpaFiles(directory=WEB_DIST, html=True), name="web")
    return app
