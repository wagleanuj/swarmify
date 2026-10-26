"""FastAPI composition root. Serves the API under /api and the built web skin at /."""

import os
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = version("swarmify-api")
WEB_DIST = Path(os.environ.get("SWARMIFY_WEB_DIST", Path(__file__).parents[4] / "apps/web/dist"))


class Health(BaseModel):
    version: str


app = FastAPI(title="Swarmify", version=VERSION)


@app.get("/api/health")
def health() -> Health:
    return Health(version=VERSION)


if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
