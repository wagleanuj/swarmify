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
MAX_SEND_BACKS = 2
"""A run may be sent back twice; the third send-back is refused (V1 spec, story 55)."""


@dataclass(frozen=True)
class RunState:
    status: RunStatus
    stages: tuple[StageStatus, ...]
    send_backs: int = 0
    over_budget: bool = False
    """The run's Budget is spent: its waiting stage is a budget gate, not a decision."""

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
    """The gate at `index` was answered with pick, approve or confirm; the run moves on."""

    index: int


@dataclass(frozen=True)
class SentBack:
    """The gate at `index` sent the run back to the earlier stage `to`, which runs again."""

    index: int
    to: int


@dataclass(frozen=True)
class BudgetSpent:
    """The running stage at `index` stopped because the run's Budget is spent. It waits at a
    budget gate: add money and it runs again, or abort."""

    index: int


@dataclass(frozen=True)
class BudgetAdded:
    """Money was added at the budget gate; the stopped stage runs again."""


@dataclass(frozen=True)
class Paused:
    pass


@dataclass(frozen=True)
class Resumed:
    pass


@dataclass(frozen=True)
class Aborted:
    pass


RunEvent = (
    Started
    | StageFinished
    | StageFailed
    | GateAnswered
    | SentBack
    | BudgetSpent
    | BudgetAdded
    | Paused
    | Resumed
    | Aborted
)


# Commands: what the shell must do next.
@dataclass(frozen=True)
class RunStage:
    """Execute a non-gate stage, then report StageFinished or StageFailed."""

    index: int


@dataclass(frozen=True)
class OpenGate:
    """Wait for a person, then report GateAnswered (or BudgetAdded at a budget gate)."""

    index: int


Command = RunStage | OpenGate


class Illegal(ValueError):
    """The event cannot happen in this state. The state is unchanged."""


class MonthSpent(Illegal):
    """The world's Monthly Budget is spent, so no run of it starts this month."""


def transition(
    kinds: tuple[str, ...], state: RunState, event: RunEvent
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
            return _advance(kinds, replace(state, status="running"))
        case StageFinished(i) | GateAnswered(i):
            expected = "waiting" if isinstance(event, GateAnswered) else "running"
            _require(not state.over_budget, "the run waits for money, not a decision")
            _require(_at(stages, i) == expected, f"stage {i} is not {expected}")
            stages[i] = "done"
            return _settle(kinds, state, stages)
        case SentBack(i, to):
            _require(_at(stages, i) == "waiting", f"stage {i} is not waiting")
            _require(not state.over_budget, "the run waits for money, not a decision")
            _require(0 <= to < i, f"send back must name a stage before {i}")
            _require(
                state.send_backs < MAX_SEND_BACKS,
                f"already sent back {MAX_SEND_BACKS} times; approve or abort",
            )
            pending: list[StageStatus] = ["pending"] * (len(stages) - to)
            stages = stages[:to] + pending
            return _settle(kinds, replace(state, send_backs=state.send_backs + 1), stages)
        case StageFailed(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "failed"
            return replace(state, status="failed", stages=tuple(stages)), []
        case BudgetSpent(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "waiting"
            status = "paused" if state.status == "paused" else "waiting_gate"
            spent = replace(state, status=status, stages=tuple(stages), over_budget=True)
            return spent, [OpenGate(i)]
        case BudgetAdded():
            _require(state.over_budget, "the run is not over budget")
            stages[stages.index("waiting")] = "pending"  # runs again, from where it stopped
            return _settle(kinds, replace(state, over_budget=False), stages)
        case Paused():
            _require(state.status in ("running", "waiting_gate"), "nothing to pause")
            return replace(state, status="paused"), []
        case Resumed():
            _require(state.status == "paused", "run is not paused")
            if "running" in stages:
                return replace(state, status="running"), []
            if "waiting" in stages:
                return replace(state, status="waiting_gate"), []
            return _advance(kinds, replace(state, status="running"))
        case Aborted():
            active = ("running", "waiting")
            ended = tuple("failed" if s in active else s for s in stages)
            return replace(state, status="aborted", stages=ended, over_budget=False), []


def _settle(
    kinds: tuple[str, ...], state: RunState, stages: list[StageStatus]
) -> tuple[RunState, list[Command]]:
    """After a stage settled: a paused run holds, a running one moves to the next stage."""
    if state.status == "paused":
        return replace(state, stages=tuple(stages)), []
    return _advance(kinds, replace(state, status="running", stages=tuple(stages)))


def _advance(kinds: tuple[str, ...], state: RunState) -> tuple[RunState, list[Command]]:
    """Activate the first pending stage, or finish the run when none is left."""
    if "pending" not in state.stages:
        return replace(state, status="done"), []
    i = state.stages.index("pending")
    stages = list(state.stages)
    if kinds[i] == "gate":
        stages[i] = "waiting"
        return replace(state, status="waiting_gate", stages=tuple(stages)), [OpenGate(i)]
    stages[i] = "running"
    return replace(state, status="running", stages=tuple(stages)), [RunStage(i)]


def _at(stages: list[StageStatus], i: int) -> StageStatus | None:
    return stages[i] if 0 <= i < len(stages) else None


def _require(ok: bool, why: str) -> None:
    if not ok:
        raise Illegal(why)
