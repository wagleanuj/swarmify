"""The real app with a scripted agent CLI behind the Agent SDK, so e2e runs need no model.

Every swarm stage takes three sessions, in order, through the real SDK client, hooks and
in-process tools: the planner seeds one task; the worker queries the graph, claims it, writes a
note and an artifact and finishes it; the judge passes with two ranked ideas. The script cycles,
so each stage gets the same three. Every run's retro session, told apart by its system prompt,
gets its own script and proposes nothing, so it never shifts the swarm's cycle.

The GraphWriter's extract writes a fixed graph instead of calling graphify's model, a second
after the last change; the real graphify MCP process answers queries over it."""

import json
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from fastapi import FastAPI

from swarmify_api.main import create_app as real_app
from swarmify_learning import SYSTEM as RETRO_SYSTEM
from swarmify_memory import GRAPH_OUT
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script, ScriptedTransport

PLANNER = [Answer({"tasks": [{"title": "Pull this week's top shorts"}]})]
WORKER = [
    Call(tool_id("query_graph"), {"question": "What do we know already?"}),
    Call(tool_id("claim_task")),
    Call("Bash", {"command": "ls"}),
    Call(tool_id("write_note"), {"title": "Findings", "body": "Hooks in the first second win."}),
    Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"}),
    Call(tool_id("finish_task"), {"task_id": LAST, "note": "Found two."}),
    Say("Done."),
]
IDEAS = [
    {
        "title": "Hook first",
        "body": "Open on the hook.",
        "rank": 1,
        "sources": [],
        "fields": {"hook": "Wait for it", "why": "Hooks in the first second win."},
    },
    {"title": "The chase", "body": "A chase in 30 seconds.", "rank": 2, "sources": []},
]
JUDGE = [Answer({"passed": True, "reasons": ["Two ideas, ranked."], "items": IDEAS})]
RETRO = [Answer({"skill": None, "workflow": None})]
"""The retro after the run proposes nothing, so the run ends without a skill gate."""


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
    swarm = Script(PLANNER, WORKER, JUDGE, cost=0.0042, cycle=True)
    retro = Script(RETRO, cost=0.0042)

    def transport(options: ClaudeAgentOptions) -> ScriptedTransport:
        return (retro if RETRO_SYSTEM in str(options.system_prompt) else swarm).transport(options)

    return real_app(
        runtime=SdkRuntime(transport=transport),
        extract=extract,
        graph_debounce=1.0,
        graph_poll=0.2,
    )
