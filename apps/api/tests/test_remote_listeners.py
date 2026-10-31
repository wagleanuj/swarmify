"""The long-lived `listen` processes of Remote Approval connectors: the core starts one per set
of Setup values with a token it provisions, scoped to `/mcp/remote_approvals`, starts it again
when it exits and stops it once no world wants it."""

import json
import shutil
from pathlib import Path

from api_fakes import until

from swarmify_api import remote
from swarmify_api.auth import Operator
from swarmify_api.main import connector_service, migrated_db
from swarmify_connectors import Packages

PAGER = Path(__file__).parents[3] / "packages/connectors/tests/fixtures/packages/pager"


def started(data: Path) -> list[dict]:
    """What each listener started with, oldest first: each writes it in its own HOME, a scratch
    folder of the connector sandbox."""
    files = sorted(data.glob("sandbox/*/home/listened.json"), key=lambda f: f.stat().st_mtime)
    return [json.loads(line) for f in files for line in f.read_text().splitlines()]


def test_the_core_keeps_one_listener_per_set_of_setup_values_running(tmp_path: Path) -> None:
    registry = connector_service(tmp_path)
    repo = tmp_path / "repo"
    shutil.copytree(PAGER, repo / "pager")
    registry.packages = Packages(tmp_path / "packages", repo)
    registry.packages.install("pager")
    folder = tmp_path
    operator = Operator(migrated_db(tmp_path))
    listeners = remote.Listeners(
        registry, lambda name: operator.provision(f"{name} remote approvals", remote.SCOPE), "u"
    )
    try:
        for world in ("w1", "w2"):
            registry.enable(world, "pager", "1.0.0")
        listeners.sync()
        assert listeners.running == {}  # no Setup yet
        registry.save_setup("w1", "pager", {"token": "t"}, {"room": "ops"})
        registry.save_setup("w2", "pager", {}, {"room": "ops"})
        listeners.sync()
        until(lambda: len(started(folder)) == 1)
        (first,) = started(folder)
        assert first["PAGER_ROOM"] == "ops" and first["SWARMIFY_MCP_URL"] == "u"
        assert operator.token_scope(first["SWARMIFY_API_TOKEN"]) == remote.SCOPE
        assert [t.name for t in operator.tokens()] == ["pager remote approvals"]

        (process,) = listeners.running.values()
        process.terminate()  # the sandbox launcher stops what it started, and exits
        process.wait()
        listeners.sync()  # it exited: started again, with the same token
        until(lambda: len(started(folder)) == 2)
        assert started(folder)[1] == first

        registry.disable("w1", "pager")
        registry.disable("w2", "pager")
        (process,) = listeners.running.values()
        listeners.sync()
        assert listeners.running == {} and process.poll() is not None
    finally:
        listeners.stop()
