"""HTTP face of a world's Graph: its size, god nodes, communities, surprising connections (each
node naming the run and note it came from), last rebuild and a plain-English query box.
Rebuilds stream as `graph.rebuilt` and `graph.failed` on the world's events (ADR 0002). A rebuild
graphify's shrink guard refused can be accepted, which reruns it over the guard; what extraction
spent is priced like an agent's tokens.

`WorldGraphs` is how agents reach the GraphIndex: worlds are named by id or name, and the swarm
wraps the tools so a stage reads only the graphs it may (ADR 0009)."""

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StringConstraints

from swarmify_memory import (
    Community,
    GodNode,
    GraphIndex,
    GraphWriter,
    Surprise,
    graph_map,
    graph_size,
)
from swarmify_swarm.sdk import price
from swarmify_worlds import Worlds


class WorldGraphs:
    def __init__(self, worlds: Worlds, index: GraphIndex) -> None:
        self.worlds = worlds
        self.index = index

    def folder(self, world: str) -> Path | None:
        found = self.worlds.get(world) or next(
            (w for w in self.worlds.list_all() if w.name == world), None
        )
        return self.worlds.folder(found.id) if found else None

    def name(self, folder: Path) -> str:
        world = self.worlds.get(folder.name)
        return world.name if world else folder.name

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        return self.index.call(folder, tool, args)


class Rebuild(BaseModel):
    at: str
    nodes: int
    edges: int
    nodes_added: int
    edges_added: int
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    """Estimated from the tokens; graphify's Claude CLI backend is billed to the plan."""


class Failure(BaseModel):
    at: str
    error: str
    shrink: bool = False
    """graphify's shrink guard refused to write a smaller graph; accepting reruns over it."""
    seconds: float = 0.0


class Spent(BaseModel):
    """Every rebuild of the world so far, refused ones included."""

    rebuilds: int
    seconds: float
    cost_usd: float


class GraphView(BaseModel):
    nodes: int
    edges: int
    god_nodes: list[GodNode]
    communities: list[Community] = []
    """The largest clusters, most connected nodes first."""
    surprises: list[Surprise] = []
    """Connections graphify finds non-obvious, across files or communities."""
    rebuilt: Rebuild | None
    """The last successful rebuild."""
    failed: Failure | None
    """The last rebuild, when it failed."""
    accepted: bool
    """A shrink was accepted and its rebuild has not finished yet."""
    spent: Spent


class Question(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Answer(BaseModel):
    answer: str


def cost(tokens: dict[str, Any]) -> float:
    """The estimated dollars of an extraction's tokens. graphify reports cache reads as input,
    and an unset GRAPHIFY_CLAUDE_CLI_MODEL is priced as the dearest model, so it errs high."""
    rates = price(os.environ.get("GRAPHIFY_CLAUDE_CLI_MODEL"))
    spent = tokens.get("input_tokens", 0) * rates["input_tokens"]
    return round((spent + tokens.get("output_tokens", 0) * rates["output_tokens"]) / 1e6, 4)


def router(worlds: Worlds, index: GraphIndex, writer: GraphWriter) -> APIRouter:
    api = APIRouter(prefix="/api/worlds/{world_id}/graph", tags=["graph"])

    def folder(world_id: str) -> Path:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")
        return worlds.folder(world_id)

    def history(world_id: str) -> GraphView:
        """The view without the graph itself: what the world's rebuild events say."""
        # ponytail: reads the world's whole event log; keep a last-rebuild row if logs grow long.
        view = GraphView(
            nodes=0,
            edges=0,
            god_nodes=[],
            rebuilt=None,
            failed=None,
            accepted=False,
            spent=Spent(rebuilds=0, seconds=0.0, cost_usd=0.0),
        )
        spent = view.spent
        for event in worlds.events_after(world_id, 0):
            if event.type == "graph.shrink_accepted":
                view.accepted = True
            elif event.type in ("graph.rebuilt", "graph.failed"):
                view.accepted, payload = False, event.payload
                spent.rebuilds += 1
                spent.seconds = round(spent.seconds + float(payload.get("seconds", 0)), 1)
                spent.cost_usd = round(spent.cost_usd + cost(payload), 4)
                if event.type == "graph.rebuilt":
                    view.rebuilt = Rebuild(at=event.ts, cost_usd=cost(payload), **payload)
                    view.failed = None
                else:
                    view.failed = Failure(at=event.ts, **payload)
        return view

    @api.get("")
    def get_graph(world_id: str) -> GraphView:
        where = folder(world_id)
        view = history(world_id)
        view.nodes, view.edges = graph_size(where)
        view.god_nodes = index.god_nodes(where)
        view.communities, view.surprises = graph_map(where)
        return view

    @api.post("/accept-shrink", status_code=202)
    def accept_shrink(world_id: str) -> None:
        """Rerun the world's rebuild once with graphify's `--allow-partial`. 409 unless its last
        rebuild was refused by the shrink guard."""
        folder(world_id)
        failed = history(world_id).failed
        if failed is None or not failed.shrink:
            raise HTTPException(409, "the last rebuild was not refused by the shrink guard")
        writer.accept_shrink(world_id)

    @api.post("/query")
    def query_graph(world_id: str, body: Question) -> Answer:
        return Answer(
            answer=index.call(folder(world_id), "query_graph", {"question": body.question})
        )

    return api
