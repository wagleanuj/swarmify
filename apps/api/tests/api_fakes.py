"""Shared helpers for the API tests: a polling wait, the recording Notifier fake, and the youtube
package's own preview and outcomes with its account signed in, for tests that publish through it
over the fake ConnectorTransport."""

import importlib.util
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swarmify_connectors.setup import SetupStore
from swarmify_kernel import Event

_spec = importlib.util.spec_from_file_location(
    "youtube_package", Path(__file__).parents[3] / "connectors/youtube/youtube.py"
)
assert _spec and _spec.loader
youtube = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(youtube)
YOUTUBE_ENV = {"YOUTUBE_ACCESS_TOKEN": "at"}
"""What the youtube package's process gets once `sign_in_youtube` has run."""


def youtube_preview(args: dict[str, Any]) -> dict[str, Any]:
    """The youtube package's `preview`, answering as its MCP tool does."""
    return youtube.preview_of(args)


def youtube_outcomes(views: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The youtube package's `outcomes`, claiming posts as its MCP tool does, every video of
    them at `views`."""
    return lambda args: {post: {"views": views} for post in youtube.claimed(args["posts"])}


def sign_in_youtube(data_dir: Path) -> None:
    """The youtube package's account signed in, as a sign-in on the Connectors page leaves it."""
    store = SetupStore(data_dir / "setup.json")
    store.put_value("youtube", None, "youtube_client_id", "id")
    store.put_value("youtube", None, "youtube_client_secret", "s")
    tokens = {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600}
    store.put_value("youtube", None, "youtube", tokens)


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def studio(client: Any, world_id: str) -> None:
    """Add `studio` to the world: a stage connector that requires nothing, standing in for any
    in tests that replace the connector stage call, so it is never started."""
    body = {
        "name": "studio",
        "kind": "stage",
        "server": "uvx studio-mcp",
        "stage_types": ["connector"],
    }
    assert client.post(f"/api/worlds/{world_id}/connectors", json=body).status_code == 201


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        self.events.append(event)


def install(client: Any, source: str) -> Any:
    """Install a package over HTTP as the Connectors page does: review it, then approve every
    permission it declares. The review's answer when it refuses."""
    review = client.post("/api/packages/review", json={"source": source})
    if review.status_code != 200:
        return review
    approved = review.json()["permissions"]
    return client.post("/api/packages", json={"source": source, "approved": approved})
