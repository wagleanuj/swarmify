"""The real app with a scripted agent CLI behind the Agent SDK, so e2e runs need no model.

Every swarm stage takes three sessions through the real SDK client, hooks and in-process
tools, each scripted by its role, told apart by its system prompt: the planner seeds one task;
the worker queries the graph, claims it, writes a note and an artifact and finishes it; the
judge passes with two ranked ideas. Every run's retro session proposes nothing, or, with
`SWARMIFY_E2E_RETRO=propose` in the app's environment, one new skill. A `remotion_build` stage's
builder session installs packages and writes src/Main.tsx.

The planner chat, told apart by its system prompt too, drafts GOLDEN. The built-in Remotion
render, speech and YouTube connectors answer from a fake ConnectorTransport (render writes the
video and its sidecar, as the real wrapper does; speech writes timings.json and a silent take per
scene; YouTube counts 1,234 views); a connector a test adds goes through the real MCP transport.
Upkeep runs every second.

The GraphWriter's extract writes a fixed graph instead of calling graphify's model, a second
after the last change; the real graphify MCP process answers queries over it."""

import json
import os
import wave
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from fastapi import FastAPI

from swarmify_api.main import create_app as real_app
from swarmify_api.planner import SYSTEM as PLANNER_SYSTEM
from swarmify_connectors import Connector, FakeTransport, McpTransport
from swarmify_learning import SYSTEM as RETRO_SYSTEM
from swarmify_memory import GRAPH_OUT
from swarmify_swarm import SdkRuntime, tool_id
from swarmify_swarm.build import SYSTEM as BUILDER_SYSTEM
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
BUILDER = [
    Call("Bash", {"command": "npm install"}),
    Call("Write", {"file_path": "src/Main.tsx", "content": "export const Main = () => null;"}),
    Say("A title card over the narration."),
]
RETRO = [Answer({"skill": None, "workflow": None})]
"""The retro after the run proposes nothing, so the run ends without a skill gate."""
HOOKS = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n"
PROPOSE = [
    Answer(
        {
            "skill": {
                "action": "new",
                "name": "hooks",
                "content": HOOKS,
                "summary": "Open every short on its hook.",
                "evidence": ["research: the judge passed the hook-first idea as #1."],
            },
            "workflow": None,
        }
    )
]

GOLDEN = """name: Weekly short
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week.
    done_when: 5 ideas ranked
    output_type: list[idea]
    produces: [ideas.md]
  - gate: pick
    ask: Pick one idea.
    kind: pick
  - connector: render
    calls: remotion_render
    input: ideas.md
    output: short.mp4
    params: {project: /tmp, composition: Short}
  - gate: review
    ask: Watch it. Approve or send back.
    kind: approve
  - gate: confirm
    ask: Post it?
    kind: confirm
  - connector: publish
    calls: youtube
    input: short.mp4
    params: {title: Five hooks that work, visibility: unlisted}
"""
PLANNER_CHAT = [
    Answer(
        {
            "reply": "Research, you pick, render, review, then post after you confirm.",
            "yaml": GOLDEN,
        }
    )
]
VIDEO = "e2e-video"
SCENES = [("hook", 1.5, True), ("payoff", 2.0, False)]
"""The speech fake's takes: scene, seconds, and whether its first line runs into the next."""


def voice(out: Path) -> dict[str, Any]:
    """What the speech connector leaves: timings.json and a silent take per scene beside it."""
    (out.parent / "narration").mkdir(exist_ok=True)
    takes = []
    for scene, seconds, joined in SCENES:
        with wave.open(str(out.parent / f"narration/{scene}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * int(8000 * seconds))
        lines = [{"text": "One.", "joined_next": joined}, {"text": "Two.", "joined_next": False}]
        takes.append(
            {"scene": scene, "file": f"narration/{scene}.wav", "duration": seconds, "lines": lines}
        )
    out.write_text(json.dumps({"engine": "http://localhost:8881", "takes": takes}))
    total = sum(s for _, s, _ in SCENES)
    return {
        "output": str(out),
        "takes": len(takes),
        "seconds": total,
        "message": f"{len(takes)} takes",
    }


class Transport(McpTransport):
    """The real MCP transport, but the built-in render and YouTube connectors are fakes."""

    def __init__(self) -> None:
        super().__init__()
        self.fake = FakeTransport(
            outputs={
                "youtube": [{"video_id": VIDEO, "url": f"https://youtu.be/{VIDEO}"}],
                "views": [{"views": {VIDEO: 1234}}],
            }
        )

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        if connector.name == "remotion_render":
            out = Path(arguments["output_path"])
            out.write_bytes(b"not really an mp4")
            head = {**arguments["frontmatter"], "type": "video", "file": out.name}
            front = "\n".join(f"{k}: {json.dumps(v)}" for k, v in head.items())
            out.with_name(f"{out.name}.md").write_text(f"---\n{front}\n---\n\n# {out.name}\n")
            return self.fake.call(connector, tool, arguments, env) | {"output": out.name}
        if connector.name == "youtube":
            return self.fake.call(connector, tool, arguments, env)
        if connector.name == "speech":
            return voice(Path(arguments["output_path"]))
        return super().call(connector, tool, arguments, env)


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


def extract(folder: Path, allow_shrink: bool = False) -> None:
    (folder / GRAPH_OUT).mkdir(exist_ok=True)
    (folder / GRAPH_OUT / "graph.json").write_text(json.dumps(GRAPH))


def create_app() -> FastAPI:
    retro = PROPOSE if os.environ.get("SWARMIFY_E2E_RETRO") == "propose" else RETRO
    scripts = {  # the first whose marker is in the system prompt plays the session
        PLANNER_SYSTEM: Script(PLANNER_CHAT, cost=0.0042),
        RETRO_SYSTEM: Script(retro, cost=0.0042),
        BUILDER_SYSTEM: Script(BUILDER, cost=0.0042),
        "You are the planner of a swarm": Script(PLANNER, cost=0.0042),
        "You are the judge of a swarm": Script(JUDGE, cost=0.0042),
        "a worker in a swarm": Script(WORKER, cost=0.0042),
    }

    def transport(options: ClaudeAgentOptions) -> ScriptedTransport:
        prompt = str(options.system_prompt)
        script = next(s for marker, s in scripts.items() if marker in prompt)
        return script.transport(options)

    return real_app(
        transport=Transport(),
        runtime=SdkRuntime(transport=transport),
        extract=extract,
        graph_debounce=1.0,
        graph_poll=0.2,
        upkeep_seconds=1.0,  # view counts and retention every second, not every six hours
    )
