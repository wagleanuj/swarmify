"""Boot the real API, serving the built web skin, for Playwright end-to-end tests."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).parents[2]
WEB_DIST = REPO / "apps/web/dist"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("swarmify")


@contextmanager
def _serve(data_dir: Path, **env: str) -> Iterator[str]:
    """The scripted app under uvicorn on a free port, with `env` added to its environment."""
    if not (WEB_DIST / "index.html").exists():
        pytest.fail(f"{WEB_DIST} is missing; run `pnpm --filter web build` first")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "SWARMIFY_WEB_DIST": str(WEB_DIST),
        "SWARMIFY_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(Path(__file__).parent),
        **env,
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "--factory",
        "scripted_app:create_app",
        "--port",
        str(port),
    ]
    with subprocess.Popen(cmd, env=env) as server:
        try:
            for _ in range(100):
                try:
                    httpx.get(f"{url}/api/health").raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                pytest.fail("API did not come up")
            yield url
        finally:
            server.terminate()


@pytest.fixture(scope="session")
def base_url(data_dir: Path) -> Iterator[str]:
    with _serve(data_dir) as url:
        yield url


@pytest.fixture(scope="session")
def locked_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A second app behind basic auth, with its own data folder."""
    data = tmp_path_factory.mktemp("swarmify-locked")
    with _serve(data, SWARMIFY_USER="erlich", SWARMIFY_PASSWORD="aviato") as url:
        yield url


@pytest.fixture
def serve() -> Callable[..., AbstractContextManager[str]]:
    """Start an app of its own: `with serve(data_dir, **env) as url`. For a test that restarts
    the server, sets its environment, or leaves the shared script's session cycle shifted."""
    return _serve


def _until(check: Callable[[], object]) -> None:
    for _ in range(200):
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def until() -> Callable[[Callable[[], object]], None]:
    """Polls `check` every 0.1 s for up to 20 s, for state no locator shows."""
    return _until
