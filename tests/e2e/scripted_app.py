"""The real app with a scripted agent CLI behind the Agent SDK, so e2e runs need no model.

Every session queries the graph, claims the next task, writes a note and an artifact and
finishes the task, through the real SDK client, hooks and in-process tools."""

from fastapi import FastAPI

from swarmify_api.main import create_app as real_app
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


def create_app() -> FastAPI:
    return real_app(runtime=SdkRuntime(transport=Script(SESSION, cost=0.0042).transport))
