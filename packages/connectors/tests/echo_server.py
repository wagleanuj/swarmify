"""A tiny stdio MCP connector for tests: `python echo_server.py <state_dir>`.

Its long job keeps state in files under `state_dir`, as a real wrapper must, because the service
opens one session per call and poll.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

STATE = Path(".")
mcp = MCPServer("echo")


@mcp.tool(description="Answer with the text it was given.")
def echo(text: str = "", idempotency_key: str = "") -> dict[str, Any]:
    return {"text": text, "idempotency_key": idempotency_key}


@mcp.tool(description="Tell whether an environment variable is set, and the leak probe.")
def env(name: str) -> dict[str, Any]:
    return {"value": os.environ.get(name), "leak": os.environ.get("SWARMIFY_LEAK_PROBE")}


@mcp.tool(description="Always fails.")
def fail() -> dict[str, Any]:
    raise ToolError("the echo server said no")


@mcp.tool(description="Start a render job, once per idempotency key.")
def render(idempotency_key: str, output: str = "") -> dict[str, Any]:
    job = STATE / f"{idempotency_key}.json"
    if not job.exists():
        job.write_text(json.dumps({"polls": 0, "output": output}))
    return {"job_id": idempotency_key, "poll": "render_status", "progress": 0}


@mcp.tool(description="Poll a render job. Done after two polls.")
def render_status(job_id: str) -> dict[str, Any]:
    job = STATE / f"{job_id}.json"
    state = json.loads(job.read_text())
    state["polls"] += 1
    job.write_text(json.dumps(state))
    if state["polls"] >= 2:
        return {"status": "done", "progress": 1, "output": state["output"]}
    return {"status": "running", "progress": 0.5, "message": "rendering frames"}


if __name__ == "__main__":
    STATE = Path(sys.argv[1]) if len(sys.argv) > 1 else STATE
    mcp.run("stdio")
