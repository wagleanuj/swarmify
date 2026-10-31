"""Serve the app with a seeded demo world and no model, for design passes and screenshots.

Boots the e2e scripted app (fake agent transport, fixed graph, see tests/e2e/scripted_app.py)
on a fresh temp data folder, then seeds the Video world: a Weekly short workflow, a run of it
waiting at its pick gate, a Trend sweep workflow with a finished run, and a monthly budget.
The scripted swarm cycles planner, worker, judge, so a baseline (which skips the
planner) cannot be seeded. Serves the built skin, so run `pnpm --filter web build` first.

    uv run python tools/demo.py [--port 8000]
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).parents[1]
WEEKLY = """name: Weekly short
budget_usd: 5
agents: 4
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week. Check the graph for what we published.
    done_when: 5 ideas ranked, each with a hook line and a why
    tools: [web_search, graph]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea. Optional note for the script.
    kind: pick
  - swarm: script
    goal: Write the script and shot list for the pick, 20 to 25 seconds.
    done_when: script.md and shots.md are written
    tools: [graph]
  - gate: review
    ask: Read the script. Approve or send back with a note.
    kind: approve
"""
SWEEP = """name: Trend sweep
budget_usd: 2
stages:
  - swarm: research
    goal: What is trending in the attraction niche this week, in one short brief.
    done_when: one brief with five trends
    tools: [web_search]
"""


def wait_for(api: httpx.Client, run_id: str, status: str) -> None:
    for _ in range(300):
        if api.get(f"/runs/{run_id}").json()["status"] == status:
            return
        time.sleep(0.2)
    raise SystemExit(f"run {run_id} never reached {status}")


def seed(api: httpx.Client) -> str:
    video = {"name": "Video", "purpose": "Weekly shorts for the attraction channel."}
    research = {"name": "Marketing research", "purpose": "Who is winning the niche this month."}
    world = api.post("/worlds", json=video).json()
    api.post("/worlds", json=research)
    api.patch(f"/worlds/{world['id']}", json={"monthly_budget_usd": 60})
    workflows = f"/worlds/{world['id']}/workflows"
    sweep = api.post(workflows, json={"yaml": SWEEP}).json()["workflow_id"]
    wait_for(api, api.post(f"/workflows/{sweep}/runs", json={}).json()["id"], "done")
    weekly = api.post(workflows, json={"yaml": WEEKLY}).json()["workflow_id"]
    wait_for(api, api.post(f"/workflows/{weekly}/runs", json={}).json()["id"], "waiting_gate")
    return world["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the app with a seeded demo world.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    data = Path(tempfile.mkdtemp(prefix="swarmify-demo-"))
    env = {**os.environ, "SWARMIFY_DATA_DIR": str(data), "PYTHONPATH": str(REPO / "tests/e2e")}
    app = "scripted_app:create_app"
    cmd = [sys.executable, "-m", "uvicorn", "--factory", app, "--port", str(args.port)]
    url = f"http://127.0.0.1:{args.port}"
    with subprocess.Popen(cmd, env=env) as server:
        try:
            api = httpx.Client(base_url=f"{url}/api", timeout=10)
            for _ in range(100):
                try:
                    api.get("/health").raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise SystemExit("the API did not come up")
            world = seed(api)
            print(f"\nDemo world at {url}/worlds/{world} (data in {data}). Ctrl-C stops it.\n")
            server.wait()
        finally:
            server.terminate()


if __name__ == "__main__":
    main()
