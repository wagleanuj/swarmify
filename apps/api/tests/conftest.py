import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_api.main import create_app
from swarmify_connectors import FakeTransport


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def app(tmp_path: Path, transport: FakeTransport) -> FastAPI:
    """The API with the fake ConnectorTransport behind it."""
    return create_app(tmp_path, transport=transport, poll_seconds=0.01)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def live_url(app: FastAPI) -> Iterator[str]:
    """A real uvicorn server in a thread. TestClient buffers bodies, so SSE needs this."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
