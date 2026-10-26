"""Boot the real API, serving the built web skin, for Playwright end-to-end tests."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
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
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not (WEB_DIST / "index.html").exists():
        pytest.fail(f"{WEB_DIST} is missing; run `pnpm --filter web build` first")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    data_dir = tmp_path_factory.mktemp("swarmify")
    env = {**os.environ, "SWARMIFY_WEB_DIST": str(WEB_DIST), "SWARMIFY_DATA_DIR": str(data_dir)}
    cmd = [sys.executable, "-m", "uvicorn", "swarmify_api.main:app", "--port", str(port)]
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
