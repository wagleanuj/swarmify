"""The connector sandbox through the API (ADR 0021): a connector stage whose connector tries to
leave its sandbox is refused each time, and each block lands in the stage's run log under the
connector's name. Real DBOS on SQLite, the real MCP transport, the intruder fixture added on the
Connectors page as a local package."""

import shlex
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from swarmify_api.main import create_app

INTRUDER = Path(__file__).parents[3] / "packages/connectors/tests/fixtures/packages/intruder"


def test_a_connector_leaving_its_sandbox_is_blocked_and_each_block_is_in_its_run_log(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    (data / ".env").parent.mkdir(parents=True)
    (data / ".env").write_text("UPLOAD_POST_API_KEY=another-packages-key\n")
    (data / ".env").chmod(0o600)
    with TestClient(create_app(data, poll_seconds=0.05, retro=False)) as client:
        w = client.post("/api/worlds", json={"name": "Sandboxed"}).json()["id"]
        other = client.post("/api/worlds", json={"name": "Another"}).json()["id"]
        server = shlex.join([sys.executable, str(INTRUDER / "server.py")])
        body = {"name": "intruder", "kind": "stage", "server": server}
        body |= {"stage_types": ["connector"], "network": ["api.example.org"]}
        added = client.post(f"/api/worlds/{w}/connectors", json=body)
        assert added.status_code == 201, added.text
        params = f"{{tool: trespass, data_dir: '{data}', other_world: {other}"
        params += ", other_key: UPLOAD_POST_API_KEY, address: '203.0.113.7:80'}"
        stages = f"  - connector: try\n    calls: intruder\n    params: {params}\n"
        yaml = f"name: Trespass\nstages:\n{stages}"
        saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
        assert saved.status_code == 201, saved.text
        run = client.post(f"/api/workflows/{saved.json()['workflow_id']}/runs", json={}).json()
        deadline = time.monotonic() + 60
        while (run := client.get(f"/api/runs/{run['id']}").json())["status"] not in (
            "done",
            "failed",
        ):
            assert time.monotonic() < deadline, run
            time.sleep(0.1)
        assert run["status"] == "done", run
        stage = run["stages"][0]["id"]
        tried = client.get(f"/api/stages/{stage}/call").json()["response"]
        log = client.get(f"/api/runs/{run['id']}/stages/{stage}/log").json()

    assert all(said.startswith("refused") for said in tried.values()), tried
    assert "another-packages-key" not in str(tried)
    blocked = [e["detail"] for e in log if e["agent"] == "intruder" and e["kind"] == "denied"]
    proxy = [b for b in blocked if b.startswith("Blocked by the sandbox: connect example.com:80")]
    assert proxy, blocked  # the proxy's own, reported everywhere
    folder = data.resolve()
    expected = {f"read {folder / 'worlds' / other}", f"read {folder / 'setup.json'}"}
    expected.add("connect 203.0.113.7:80" if sys.platform == "linux" else "connect *:80")
    said = {b.removeprefix("Blocked by the sandbox: ") for b in blocked if b not in proxy}
    # macOS reports Seatbelt's blocks through its log, which drops some under load
    assert said <= expected if sys.platform == "darwin" else expected <= said, (said, expected)
