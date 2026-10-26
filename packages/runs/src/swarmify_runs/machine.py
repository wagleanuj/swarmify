"""The stage state machine: a pure, synchronous function from (stage kinds, state, event) to
(next state, commands). The DBOS workflow is a thin shell that executes the commands and feeds
their outcomes back as events (V1 spec, Runs context).
"""

from dataclasses import dataclass, replace
from typing import Literal

RunStatus = Literal["queued", "running", "waiting_gate", "paused", "done", "failed", "aborted"]
StageStatus = Literal["pending", "running", "waiting", "done", "failed", "skipped"]
RUN_STATUSES: tuple[RunStatus, ...] = (
    "queued", "running", "waiting_gate", "paused", "done", "failed", "aborted"
)  # fmt: skip
STAGE_STATUSES: tuple[StageStatus, ...] = (
    "pending", "running", "waiting", "done", "failed", "skipped"
)  # fmt: skip
TERMINAL: frozenset[RunStatus] = frozenset({"done", "failed", "aborted"})


@dataclass(frozen=True)
class RunState:
    status: RunStatus
    stages: tuple[StageStatus, ...]

    @staticmethod
    def new(stage_count: int) -> "RunState":
        return RunState("queued", ("pending",) * stage_count)


# Events: what happened.
@dataclass(frozen=True)
class Started:
    pass


@dataclass(frozen=True)
class StageFinished:
    index: int


@dataclass(frozen=True)
class StageFailed:
    index: int


@dataclass(frozen=True)
class GateAnswered:
    index: int


@dataclass(frozen=True)
class Paused:
    pass


@dataclass(frozen=True)
class Resumed:
    pass


@dataclass(frozen=True)
class Aborted:
    pass


Event = Started | StageFinished | StageFailed | GateAnswered | Paused | Resumed | Aborted


# Commands: what the shell must do next.
@dataclass(frozen=True)
class RunStage:
    """Execute a non-gate stage, then report StageFinished or StageFailed."""

    index: int


@dataclass(frozen=True)
class OpenGate:
    """Wait for a person, then report GateAnswered."""

    index: int


Command = RunStage | OpenGate


class Illegal(ValueError):
    """The event cannot happen in this state. The state is unchanged."""


def transition(
    kinds: tuple[str, ...], state: RunState, event: Event
) -> tuple[RunState, list[Command]]:
    """`kinds` are the stage types in order (swarm, gate, connector, workflow)."""
    if len(kinds) != len(state.stages):
        raise ValueError("kinds and stages differ in length")
    if state.status in TERMINAL:
        raise Illegal(f"run is {state.status}")
    stages = list(state.stages)
    match event:
        case Started():
            _require(state.status == "queued", "only a queued run starts")
            return _advance(kinds, RunState("running", state.stages))
        case StageFinished(i) | GateAnswered(i):
            expected = "waiting" if isinstance(event, GateAnswered) else "running"
            _require(_at(stages, i) == expected, f"stage {i} is not {expected}")
            stages[i] = "done"
            if state.status == "paused":
                return RunState("paused", tuple(stages)), []
            return _advance(kinds, RunState("running", tuple(stages)))
        case StageFailed(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "failed"
            return RunState("failed", tuple(stages)), []
        case Paused():
            _require(state.status in ("running", "waiting_gate"), "nothing to pause")
            return RunState("paused", state.stages), []
        case Resumed():
            _require(state.status == "paused", "run is not paused")
            if "running" in stages:
                return RunState("running", state.stages), []
            if "waiting" in stages:
                return RunState("waiting_gate", state.stages), []
            return _advance(kinds, RunState("running", state.stages))
        case Aborted():
            active = ("running", "waiting")
            return RunState("aborted", tuple("failed" if s in active else s for s in stages)), []


def _advance(kinds: tuple[str, ...], state: RunState) -> tuple[RunState, list[Command]]:
    """Activate the first pending stage, or finish the run when none is left."""
    if "pending" not in state.stages:
        return replace(state, status="done"), []
    i = state.stages.index("pending")
    stages = list(state.stages)
    if kinds[i] == "gate":
        stages[i] = "waiting"
        return RunState("waiting_gate", tuple(stages)), [OpenGate(i)]
    stages[i] = "running"
    return RunState("running", tuple(stages)), [RunStage(i)]


def _at(stages: list[StageStatus], i: int) -> StageStatus | None:
    return stages[i] if 0 <= i < len(stages) else None


def _require(ok: bool, why: str) -> None:
    if not ok:
        raise Illegal(why)
