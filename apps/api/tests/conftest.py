import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_graph import fake_extract
from swarmify_api.main import create_app
from swarmify_connectors import FakeTransport
from swarmify_memory import FakeGraphIndex
from swarmify_swarm import SdkRuntime
from swarmify_swarm.fake import Script


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def script() -> Script:
    """What the scripted CLI does in each agent session; a test replaces `scripts`."""
    return Script()


@pytest.fixture
def graph_index() -> FakeGraphIndex:
    return FakeGraphIndex()


@pytest.fixture
def app(
    tmp_path: Path, transport: FakeTransport, script: Script, graph_index: FakeGraphIndex
) -> FastAPI:
    """The API with the fake ConnectorTransport, a scripted agent CLI, the fake GraphIndex and
    a GraphWriter whose extract writes a fixed graph."""
    return create_app(
        tmp_path,
        transport=transport,
        poll_seconds=0.01,
        runtime=SdkRuntime(transport=script.transport),
        graph_index=graph_index,
        extract=fake_extract,
    )


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
