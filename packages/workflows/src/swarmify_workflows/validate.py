"""The rules a workflow must satisfy beyond its schema (V1 spec, Workflows context; ADR 0007, 0013).

Pure: the caller supplies the connectors the world allows and, for every stored workflow, the
workflow ids its current version runs. Every broken rule is returned, none raises.
"""

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from swarmify_workflows.model import (
    ConnectorRef,
    ConnectorStage,
    GateStage,
    SwarmStage,
    Violation,
    WorkflowModel,
    WorkflowStage,
)

MAX_DEPTH = 2
# Explicit, not mimetypes: that marks text like .srt, .ts and .sh as binary and reads the host's
# /etc/mime.types, so the same workflow could pass on one machine and fail on another.
BINARY_EXTENSIONS = frozenset(
    "mp4 mov webm mkv avi m4v mp3 wav m4a aac ogg flac png jpg jpeg gif webp heic bmp tiff ico"
    " pdf zip tar gz tgz 7z rar".split()
)


def _binary(name: str) -> bool:
    """Swarm agents write text only; an unlisted extension gets the benefit of the doubt."""
    return PurePosixPath(name).suffix[1:].lower() in BINARY_EXTENSIONS


def validate(
    model: WorkflowModel,
    *,
    workflow_id: str | None,
    connectors: Mapping[str, ConnectorRef],
    references: Mapping[str, Sequence[str]],
) -> list[Violation]:
    out: list[Violation] = []
    produced: set[str] = set()

    def check_connector(name: str, stage_type: str, stage: str) -> ConnectorRef | None:
        ref = connectors.get(name)
        if ref is None:
            out.append(Violation("unknown_connector", f"no connector named '{name}'", stage))
        elif stage_type not in ref.stage_types:
            out.append(
                Violation(
                    "connector_stage_type",
                    f"connector '{name}' is not allowed in {stage_type} stages",
                    stage,
                )
            )
        return ref

    for index, stage in enumerate(model.stages):
        if isinstance(stage, SwarmStage):
            for tool in stage.tools:
                check_connector(tool, "swarm", stage.name)
            for name in filter(_binary, stage.produces):
                out.append(
                    Violation(
                        "swarm_binary",
                        f"only a connector stage can make {name}: swarm agents write text, so"
                        " produce a script here and build the video from it in a remotion_build"
                        " stage",
                        stage.name,
                    )
                )
            produced.update(stage.produces)
        elif isinstance(stage, ConnectorStage):
            ref = check_connector(stage.calls, "connector", stage.name)
            gated = index > 0 and isinstance(model.stages[index - 1], GateStage)
            if ref is not None and ref.kind == "publish" and not gated:
                out.append(
                    Violation(
                        "publish_without_gate",
                        f"publish connector '{stage.calls}' must come right after a gate",
                        stage.name,
                    )
                )
            if stage.input and stage.input not in produced:
                out.append(
                    Violation(
                        "input_not_produced",
                        f"input '{stage.input}' is not produced by an earlier stage",
                        stage.name,
                    )
                )
            if ref is not None:
                config = {
                    "calls": stage.calls,
                    "input": stage.input,
                    "output": stage.output,
                    "params": stage.params,
                }
                out += [Violation("connector_params", p, stage.name) for p in ref.problems(config)]
            if stage.output:
                produced.add(stage.output)
        elif isinstance(stage, WorkflowStage):
            if stage.runs not in references and stage.runs != workflow_id:
                out.append(
                    Violation("unknown_workflow", f"no workflow with id '{stage.runs}'", stage.name)
                )
            produced.update(stage.take)
        # gates produce a decision, which no stage declares as input

    me = workflow_id or ""
    graph: dict[str, Sequence[str]] = dict(references)
    graph[me] = [s.runs for s in model.stages if isinstance(s, WorkflowStage)]
    if _reaches(graph, me, me):
        out.append(Violation("cycle", "this workflow would run itself through its workflow stages"))
    else:
        depth = _up(graph, me) + _down(graph, me)
        if depth > MAX_DEPTH:
            out.append(Violation("depth", f"workflows nest {depth} deep, the limit is {MAX_DEPTH}"))
    return out


def _reaches(graph: Mapping[str, Sequence[str]], start: str, target: str) -> bool:
    seen: set[str] = set()
    todo = list(graph.get(start, ()))
    while todo:
        node = todo.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(graph.get(node, ()))
    return False


def _down(graph: Mapping[str, Sequence[str]], node: str, seen: frozenset[str] = frozenset()) -> int:
    """Longest chain of workflow stages below `node`. `seen` guards against a stored cycle."""
    kids = [k for k in graph.get(node, ()) if k not in seen]
    return max((1 + _down(graph, k, seen | {node}) for k in kids), default=0)


def _up(graph: Mapping[str, Sequence[str]], node: str, seen: frozenset[str] = frozenset()) -> int:
    parents = [p for p, kids in graph.items() if node in kids and p not in seen and p != node]
    return max((1 + _up(graph, p, seen | {node}) for p in parents), default=0)
