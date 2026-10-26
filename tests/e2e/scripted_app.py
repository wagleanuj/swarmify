"""The real app with a scripted agent CLI behind the Agent SDK, so e2e runs need no model.

Every session queries the graph, claims the next task, writes a note and an artifact and
finishes the task, through the real SDK client, hooks and in-process tools.

The GraphWriter's extract writes a fixed graph instead of calling graphify's model, a second
after the last change; the real graphify MCP process answers queries over it."""

import json
from pathlib import Path

from fastapi import FastAPI

from swarmify_api.main import create_app as real_app
from swarmify_memory import GRAPH_OUT
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import Call, Say, Script

SESSION = [
    Call(tool_id("query_graph"), {"question": "What do we know already?"}),
    Call(tool_id("claim_task")),
    Call("Bash", {"command": "ls"}),
    Call(tool_id("write_note"), {"title": "Findings", "body": "Hooks in the first second win."}),
    Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"}),
    Say("Done."),
]


GRAPH = {
    "directed": False,
    "multigraph": False,
    "graph": {},
    "nodes": [
        {"id": "hooks", "label": "Short hooks", "source_file": "notes/findings.md"},
        {"id": "cliff", "label": "Retention cliff", "source_file": "notes/findings.md"},
    ],
    "links": [{"source": "hooks", "target": "cliff", "relation": "beats"}],
}


def extract(folder: Path) -> None:
    (folder / GRAPH_OUT).mkdir(exist_ok=True)
    (folder / GRAPH_OUT / "graph.json").write_text(json.dumps(GRAPH))


def create_app() -> FastAPI:
    return real_app(
        runtime=SdkRuntime(transport=Script(SESSION, cost=0.0042).transport),
        extract=extract,
        graph_debounce=1.0,
        graph_poll=0.2,
    )
