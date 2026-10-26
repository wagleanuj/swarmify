"""FastAPI composition root. Serves the API under /api and the built web skin at /.

`SWARMIFY_DATA_DIR` (default `~/.swarmify`) holds `swarmify.db` and `worlds/<id>/`.
"""

import os
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from swarmify_api import worlds
from swarmify_worlds import SqliteStorage, Worlds

VERSION = version("swarmify-api")
WEB_DIST = Path(os.environ.get("SWARMIFY_WEB_DIST", Path(__file__).parents[4] / "apps/web/dist"))


def data_dir() -> Path:
    return Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))


def world_service(root: Path) -> Worlds:
    return Worlds(SqliteStorage(root / "swarmify.db"), root)


class Health(BaseModel):
    version: str


def create_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(title="Swarmify", version=VERSION)

    @app.get("/api/health")
    def health() -> Health:
        return Health(version=VERSION)

    app.include_router(worlds.router(world_service(root or data_dir())))
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app


app = create_app()
