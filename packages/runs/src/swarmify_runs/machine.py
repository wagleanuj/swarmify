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
"""A gate may send the run back twice unless its `max_send_backs` says otherwise (V1 spec, story
55); RunControl.decide counts each gate's own send-backs and refuses the one past its limit."""


@dataclass(frozen=True)
class RunState:
    status: RunStatus
    stages: tuple[StageStatus, ...]
    send_backs: int = 0
    """Every send-back of the run so far, at any gate: a revisited stage's visit number."""
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
class StageStopped:
    """A pause or abort stopped the agents of the running stage at `index` mid-stage. The
    stage runs again, its agents resuming their sessions, once the run goes on."""

    index: int


@dataclass(frozen=True)
class GateRaised:
    """The running stage at `index` raised a gate of its own (a retro's proposal): it waits for
    a person, then GateAnswered moves the run on."""

    index: int


@dataclass(frozen=True)
class BudgetAdded:
    """Money was added at the budget gate; the stopped stage runs again."""


@dataclass(frozen=True)
class Rerun:
    """A Rerun (ADR 0027): the finished stage `to` and every later one run again, as a send-back
    from anywhere does, from a gate or a pause. A skipped stage stays skipped. `change` is the
    recorded Run Change it carries out."""

    to: int
    change: str = ""


@dataclass(frozen=True)
class Skip:
    """A Skip (ADR 0027): the stage at `index`, which has not started, is passed over."""

    index: int
    change: str = ""


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
    | StageStopped
    | GateRaised
    | BudgetAdded
    | Rerun
    | Skip
    | Paused
    | Resumed
    | Aborted
)


# Commands: what the shell must do next.
@dataclass(frozen=True)
class RunStage:
    """Execute a non-gate stage, then report StageFinished, StageFailed or GateRaised."""

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
    """`kinds` are the stage types in order (swarm, gate, connector, workflow, retro)."""
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
            _require("failed" not in stages, "a failed run cannot be sent back")
            pending: list[StageStatus] = ["pending"] * (len(stages) - to)
            stages = stages[:to] + pending
            return _settle(kinds, replace(state, send_backs=state.send_backs + 1), stages)
        case StageFailed(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "failed"
            retro = next((j for j in range(i + 1, len(kinds)) if kinds[j] == "retro"), None)
            if retro is None:
                return replace(state, status="failed", stages=tuple(stages)), []
            # A failed run still gets its retro, failures having the most to teach; the stages
            # in between are skipped and the run ends failed once the retro settles.
            for j in range(i + 1, len(stages)):
                stages[j] = "pending" if j == retro else "skipped"
            return _settle(kinds, state, stages)
        case BudgetSpent(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "waiting"
            status = "paused" if state.status == "paused" else "waiting_gate"
            spent = replace(state, status=status, stages=tuple(stages), over_budget=True)
            return spent, [OpenGate(i)]
        case StageStopped(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "pending"
            return _settle(kinds, state, stages)
        case GateRaised(i):
            _require(_at(stages, i) == "running", f"stage {i} is not running")
            stages[i] = "waiting"
            status = "paused" if state.status == "paused" else "waiting_gate"
            return replace(state, status=status, stages=tuple(stages)), [OpenGate(i)]
        case BudgetAdded():
            _require(state.over_budget, "the run is not over budget")
            stages[stages.index("waiting")] = "pending"  # runs again, from where it stopped
            return _settle(kinds, replace(state, over_budget=False), stages)
        case Rerun(to):
            at_work = state.status not in ("waiting_gate", "paused") or "running" in stages
            _require(not at_work, "the run is at work: pause first")
            _require(not state.over_budget, "the run waits at a budget gate: add money or abort")
            _require(_at(stages, to) == "done", f"stage {to} has not finished")
            to = rerun_from(kinds, stages, to)
            again: list[StageStatus] = [s if s == "skipped" else "pending" for s in stages[to:]]
            return _settle(
                kinds, replace(state, send_backs=state.send_backs + 1), stages[:to] + again
            )
        case Skip(i):
            _require(_at(stages, i) == "pending", f"stage {i} has started")
            stages[i] = "skipped"
            return replace(state, stages=tuple(stages)), []
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


def rerun_from(kinds: tuple[str, ...], stages: list[StageStatus], to: int) -> int:
    """Where a Rerun of stage `to` starts: at the answered gate right before it, if any, so the
    gate asks again and nothing it confirmed runs twice on one answer (ADR 0007)."""
    return to - 1 if to and kinds[to - 1] == "gate" and stages[to - 1] == "done" else to


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
        return replace(state, status="failed" if "failed" in state.stages else "done"), []
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
