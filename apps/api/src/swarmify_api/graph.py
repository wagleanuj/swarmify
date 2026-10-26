"""HTTP face of a world's Graph: its size, god nodes, last rebuild and a plain-English query box.
Rebuilds stream as `graph.rebuilt` and `graph.failed` on the world's events (ADR 0002).

`WorldGraphs` is how agents reach the GraphIndex: worlds are named by id or name, and the swarm
wraps the tools so a stage reads only the graphs it may (ADR 0009)."""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StringConstraints

from swarmify_memory import GodNode, GraphIndex, graph_size
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

    def call(self, folder: Path, tool: str, args: dict[str, Any]) -> str:
        return self.index.call(folder, tool, args)


class Rebuild(BaseModel):
    at: str
    nodes: int
    edges: int
    nodes_added: int
    edges_added: int
    seconds: float


class Failure(BaseModel):
    at: str
    error: str


class GraphView(BaseModel):
    nodes: int
    edges: int
    god_nodes: list[GodNode]
    rebuilt: Rebuild | None
    """The last successful rebuild."""
    failed: Failure | None
    """The last rebuild, when it failed."""


class Question(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Answer(BaseModel):
    answer: str


def router(worlds: Worlds, index: GraphIndex) -> APIRouter:
    api = APIRouter(prefix="/api/worlds/{world_id}/graph", tags=["graph"])

    def folder(world_id: str) -> Path:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")
        return worlds.folder(world_id)

    @api.get("")
    def get_graph(world_id: str) -> GraphView:
        where = folder(world_id)
        # ponytail: reads the world's whole event log; keep a last-rebuild row if logs grow long.
        rebuilt = failed = None
        for event in worlds.events_after(world_id, 0):
            if event.type == "graph.rebuilt":
                rebuilt, failed = Rebuild(at=event.ts, **event.payload), None
            elif event.type == "graph.failed":
                failed = Failure(at=event.ts, error=str(event.payload.get("error", "")))
        nodes, edges = graph_size(where)
        return GraphView(
            nodes=nodes,
            edges=edges,
            god_nodes=index.god_nodes(where),
            rebuilt=rebuilt,
            failed=failed,
        )

    @api.post("/query")
    def query_graph(world_id: str, body: Question) -> Answer:
        return Answer(
            answer=index.call(folder(world_id), "query_graph", {"question": body.question})
        )

    return api
