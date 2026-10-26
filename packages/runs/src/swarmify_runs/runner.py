"""The DBOS shell around the stage state machine (ADR 0014).

One DBOS workflow per run, id equal to the run id, enqueued on a queue partitioned by world so
each world runs at most `cap` runs at once. Every side effect is a DBOS step: loading the run,
saving each new state, executing a stage. A crash resumes from the last completed step, so a
finished stage never runs twice. DBOS is a process singleton, hence one module-level Runner.

A workflow stage starts a child run of another workflow's current version (ADR 0013), links it
to the stage and waits, like a gate, on the run's control topic: the child's workflow sends its
end there. Children wait on their own unpartitioned queue, so a parent holding one of its
world's slots never starves its own child; each parent has at most one live child, which bounds
them.

The workflow is the only writer of run state. People act through RunControl, whose messages
(decide, pause, resume, abort) arrive on the run's control topic: at a gate or while paused the
workflow waits for them; around a stage it drains what arrived meanwhile.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig, SetEnqueueOptions, SetWorkflowID

from swarmify_kernel import Event, new_id
from swarmify_runs.control import CONTROL_TOPIC, Previews, RunControl, ended_event, no_previews
from swarmify_runs.machine import (
    TERMINAL,
    Aborted,
    GateAnswered,
    Illegal,
    OpenGate,
    Paused,
    Resumed,
    RunEvent,
    RunState,
    SentBack,
    StageFailed,
    StageFinished,
    Started,
    transition,
)
from swarmify_runs.model import Pinned, Run, StageDef
from swarmify_runs.ports import Notifier, Storage

QUEUE = "runs"
CHILD_QUEUE = "child-runs"
DEFAULT_CAP = 2
MAX_DEPTH = 2
"""A child may start a child, no deeper (ADR 0013). Saving refuses deeper nesting already; this
guards a run against workflows edited after it pinned its version."""
# ponytail: pinned so a code change does not orphan runs in flight (DBOS only recovers
# workflows of the current application version). Bump it when the workflow's step order changes.
APP_VERSION = "3"

Connector = Callable[[Run, str, StageDef], None]
"""Calls one connector stage. Arguments: the run, the stage id (the idempotency key) and the
stage's definition."""
SwarmStage = Callable[[Run, str, StageDef], None]
"""Runs one swarm stage to its end, with the same arguments. Raises to fail the stage."""
Pin = Callable[[str], Pinned]
"""The current version of a workflow, by id. Raises KeyError for an unknown workflow."""
Take = Callable[[Run, Run, str, list[str]], None]
"""Copies the named artifacts of a child run into its parent's stage. Arguments: the child, the
parent, the parent's stage id and the names. Raises to fail the stage."""

_runner: "Runner | None" = None
# Runs whose workflow thread sits in recv. Shutdown wakes them, because DBOS cannot interrupt a
# recv and Python joins executor threads at exit.
_waiting: set[str] = set()
_waiting_lock = threading.Lock()
_stopping = threading.Event()
WAKE = {"swarmify": "wake"}
log = logging.getLogger(__name__)


class LogNotifier:
    """The adapter when no other is given: one log line per notification. The API passes the
    Slack adapter of the Notifications context."""

    log = logging.getLogger("swarmify.notify")

    def notify(self, event: Event) -> None:
        self.log.info("%s %s", event.type, event.payload)


@dataclass(frozen=True)
class _ChildEnded:
    """A child run reached a terminal status. Not a machine event: it becomes StageFinished
    once the artifacts are taken, or StageFailed."""

    child_run_id: str
    status: str


class _Shutdown(BaseException):
    """Leaves a workflow PENDING so the next launch recovers it. DBOS records a workflow's
    Exception as its outcome, but lets a BaseException through unrecorded, like a crash."""


class Runner(RunControl):
    def __init__(
        self,
        storage: Storage,
        system_db: Path,
        connector: Connector,
        data_dir: Path | None = None,
        notifier: Notifier | None = None,
        cap: int = DEFAULT_CAP,
        swarm: SwarmStage | None = None,
        poll_seconds: float = 1.0,
        pin: Pin | None = None,
        take: Take | None = None,
        previews: Previews = no_previews,
    ) -> None:
        """`pin` and `take` serve workflow stages; without `pin` they fail the run."""
        folder, notify = data_dir or system_db.parent, notifier or LogNotifier()
        super().__init__(storage, system_db, folder, notify, previews)
        self.connector = connector
        self.swarm = swarm
        self.pin = pin
        self.take = take
        self.cap = cap
        self.poll_seconds = poll_seconds

    def launch(self) -> None:
        """Start DBOS. Recovers runs interrupted by a crash and re-enqueues queued ones."""
        global _runner
        _runner = self
        _stopping.clear()
        config: DBOSConfig = {
            "name": "swarmify",
            "system_database_url": f"sqlite:///{self.system_db}",
            "application_version": APP_VERSION,
            "notification_listener_polling_interval_sec": min(self.poll_seconds, 1.0),
            "log_level": "WARNING",
        }
        DBOS(config=config)
        DBOS.launch()
        # ponytail: one partitioned queue gives every world the same cap. Per-world caps need a
        # queue per world, registered as worlds are created.
        DBOS.register_queue(
            QUEUE,
            partition_concurrency=self.cap,
            polling_interval_sec=self.poll_seconds,
            on_conflict="always_update",
        )
        DBOS.register_queue(
            CHILD_QUEUE, polling_interval_sec=self.poll_seconds, on_conflict="always_update"
        )
        for run_id in self.storage.queued_run_ids():
            run = self.storage.get_run(run_id)
            if run is not None:
                self._enqueue(run)

    def shutdown(self, seconds: float = 5.0) -> None:
        """Release the waiting workflows, then stop DBOS. Interrupted runs resume on launch."""
        global _runner
        _stopping.set()
        with _waiting_lock:
            waiting = list(_waiting)
        for run_id in waiting:
            DBOS.send(run_id, WAKE, topic=CONTROL_TOPIC)
        deadline = time.monotonic() + seconds
        while _waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.close()
        DBOS.destroy()
        _runner = None

    def start(
        self,
        world_id: str,
        workflow_id: str,
        version: int,
        name: str,
        stages: list[StageDef],
        parent_run_id: str | None = None,
    ) -> Run:
        """Record a queued run pinned to `version` and enqueue it."""
        now = datetime.now(UTC).isoformat()
        run = Run(
            new_id("run"), world_id, workflow_id, version, name, "queued", now, now, [],
            parent_run_id=parent_run_id,
        )  # fmt: skip
        run = self.storage.add_run(run, stages)
        self._enqueue(run)
        return run

    def list_all(self, world_id: str) -> list[Run]:
        return self.storage.list_runs(world_id)

    def delete_world(self, world_id: str) -> None:
        """Cancel the world's unfinished runs, then drop its rows."""
        for run_id in self.storage.active_run_ids(world_id):
            DBOS.cancel_workflow(run_id)
            DBOS.send(run_id, WAKE, topic=CONTROL_TOPIC)
        self.storage.delete_world(world_id)

    def _enqueue(self, run: Run) -> None:
        # The run id is the workflow id, so enqueueing twice attaches to the same workflow.
        if run.parent_run_id is not None:
            with SetWorkflowID(run.id):
                DBOS.enqueue_workflow(CHILD_QUEUE, execute_run, run.id)
            return
        with SetWorkflowID(run.id), SetEnqueueOptions(queue_partition_key=run.world_id):
            DBOS.enqueue_workflow(QUEUE, execute_run, run.id)


def _current() -> Runner:
    assert _runner is not None, "Runner.launch() was not called"
    return _runner


@DBOS.workflow(name="swarmify.run")
def execute_run(run_id: str) -> str:
    kinds, status, stages, send_backs, parent = _load(run_id)
    if status in TERMINAL:  # aborted between dequeue and load: nothing to run or wait for
        return status
    state = RunState(status, tuple(stages), send_backs)  # type: ignore[arg-type]  # CHECKed rows
    events: list[RunEvent | _ChildEnded] = [*_drain(run_id), Started()]
    error: str | None = None
    while True:
        if not events:  # at a gate, paused, waiting on a child, or after a stale message
            events = [_as_event(_wait(run_id))]
        event, events = events[0], events[1:]
        if isinstance(event, _ChildEnded):
            taken = _take(run_id, event.child_run_id, event.status)
            if taken is None:
                continue  # a child of an earlier visit to the stage
            index, error = taken
            event = StageFinished(index) if error is None else StageFailed(index)
        try:
            state, commands = transition(tuple(kinds), state, event)
        except Illegal:
            continue  # a stale message, e.g. a resume for a run that is not paused
        _save(run_id, state, error, bool(commands and isinstance(commands[0], OpenGate)))
        if state.status in TERMINAL:
            if parent is not None:
                message = {"action": "child", "child": run_id, "status": state.status}
                DBOS.send(parent, message, topic=CONTROL_TOPIC)
            return state.status
        if events or not commands or isinstance(commands[0], OpenGate):
            continue
        index, error = commands[0].index, None
        if kinds[index] == "workflow":
            try:
                child = _start_child(run_id, index)
            except Exception as exc:
                events = [*_drain(run_id), StageFailed(index)]
                error = f"{type(exc).__name__}: {exc}"
                continue
            with SetWorkflowID(child):  # a workflow-level op, so a replay never enqueues twice
                DBOS.enqueue_workflow(CHILD_QUEUE, execute_run, child)
            events = list(_drain(run_id))  # then wait for the child's end on the control topic
            continue
        try:
            _execute(run_id, index)
            outcome: RunEvent = StageFinished(index)
        except Exception as exc:  # a failed stage fails the run; the error lands on the stage
            outcome, error = StageFailed(index), f"{type(exc).__name__}: {exc}"
        # ponytail: a pause or abort sent mid-stage lands once the stage finishes. Cancelling a
        # stage midway arrives with long swarm stages (T07).
        events = [*_drain(run_id), outcome]


def _as_event(message: dict[str, Any]) -> RunEvent | _ChildEnded:
    match message["action"]:
        case "child":
            return _ChildEnded(message["child"], message["status"])
        case "decide" if "to" in message:
            return SentBack(message["index"], message["to"])
        case "decide":
            return GateAnswered(message["index"])
        case "pause":
            return Paused()
        case "resume":
            return Resumed()
        case _:
            return Aborted()


def _drain(run_id: str) -> list[RunEvent | _ChildEnded]:
    """Every control message that arrived while the workflow was busy, in order."""
    events: list[RunEvent | _ChildEnded] = []
    while (message := DBOS.recv(CONTROL_TOPIC, timeout_seconds=0)) is not None:
        if message != WAKE:
            events.append(_as_event(message))
    return events


def _wait(run_id: str) -> dict[str, Any]:
    with _waiting_lock:
        _waiting.add(run_id)
    try:
        while True:
            message = DBOS.recv(CONTROL_TOPIC, timeout_seconds=3600)
            if message == WAKE and _stopping.is_set():
                raise _Shutdown(run_id)
            if message is not None and message != WAKE:
                return message
    finally:
        with _waiting_lock:
            _waiting.discard(run_id)


@DBOS.step(name="swarmify.load")
def _load(run_id: str) -> tuple[list[str], str, list[str], int, str | None]:
    run = _current().storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    kinds, stages = [s.type for s in run.stages], [s.status for s in run.stages]
    return kinds, run.status, stages, run.send_backs, run.parent_run_id


@DBOS.step(name="swarmify.start_child")
def _start_child(run_id: str, index: int) -> str:
    """Record the child run of the workflow stage at `index` and link it, once per visit: a
    retried step finds the link and returns the same child."""
    runner = _current()
    run = runner.storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    if (child := run.stages[index].child) is not None:
        return child.id
    if runner.pin is None:
        raise NotImplementedError("workflow stages need a pin to find their workflow")
    depth, above = 1, run.parent_run_id
    while above is not None:
        depth, parent = depth + 1, runner.storage.get_run(above)
        above = parent.parent_run_id if parent else None
    if depth > MAX_DEPTH:
        raise ValueError(f"child runs nest at most {MAX_DEPTH} deep")
    _, stage = runner.storage.stage_def(run_id, index)
    pinned = runner.pin(str(stage.config.get("runs") or ""))
    now = datetime.now(UTC).isoformat()
    child = Run(
        new_id("run"), pinned.world_id, pinned.workflow_id, pinned.version, pinned.name,
        "queued", now, now, [], parent_run_id=run_id,
    )  # fmt: skip
    child = runner.storage.add_run(child, pinned.stages)
    runner.storage.set_child(run_id, index, child.id)
    return child.id


@DBOS.step(name="swarmify.take")
def _take(run_id: str, child_run_id: str, status: str) -> tuple[int, str | None] | None:
    """The stage a child ended for and its error, after copying the artifacts it names into the
    stage. None when no running stage waits on this child."""
    runner = _current()
    run = runner.storage.get_run(run_id)
    child = runner.storage.get_run(child_run_id)
    stage = next(
        (s for s in (run.stages if run else []) if s.child and s.child.id == child_run_id),
        None,
    )
    if run is None or child is None or stage is None or stage.status != "running":
        return None
    if status != "done":
        return stage.index, f"child run {child.name} ended {status}"
    names = [
        str(n) for n in runner.storage.stage_def(run_id, stage.index)[1].config.get("take") or []
    ]
    try:
        if names:
            if runner.take is None:
                raise NotImplementedError("taking artifacts needs a take")
            runner.take(child, run, stage.id, names)
    except Exception as exc:
        return stage.index, f"{type(exc).__name__}: {exc}"
    return stage.index, None


@DBOS.step(name="swarmify.save")
def _save(run_id: str, state: RunState, error: str | None, gate_opened: bool) -> None:
    """Save the state; when a gate opened or the run ended, record the event and notify."""
    # ponytail: one step, so a crash between save, record and notify replays all three and the
    # world stream gets the run.updated and gate.opened or run.ended events twice. Split into
    # steps, or key events by (run, state), if duplicates start to matter.
    runner = _current()
    run = runner.storage.save_state(run_id, state, error)
    if run is None:
        return
    if gate_opened:
        index = state.stages.index("waiting")
        _, stage = runner.storage.stage_def(run_id, index)
        shown = runner.preview_after(run_id, index, run.world_id)
        event = Event.new(
            "gate.opened",
            world_id=run.world_id,
            run_id=run.id,
            payload={
                "run_id": run.id,
                "name": run.name,
                "stage_index": index,
                "stage": stage.name,
                "kind": stage.config.get("kind") or "approve",
                "ask": stage.config.get("ask") or "",
                "publish": asdict(shown) if shown else None,
            },
        )
    elif state.status in TERMINAL:
        event = ended_event(run)
    else:
        return
    runner.storage.record(event)
    try:
        runner.notifier.notify(event)
    except Exception:  # a notifier outage (Slack) must not fail the run; the event is recorded
        log.exception("notifier failed on %s for run %s", event.type, run_id)


@DBOS.step(name="swarmify.execute")
def _execute(run_id: str, index: int) -> None:
    runner = _current()
    stage_id, stage = runner.storage.stage_def(run_id, index)
    call = {"connector": runner.connector, "swarm": runner.swarm}.get(stage.type)
    if call is None:
        raise NotImplementedError(f"{stage.type} stages cannot run yet")
    run = runner.storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    if stage.type == "connector" and runner.previews(run.world_id, stage) is not None:
        before = run.stages[index - 1] if index > 0 else None
        if before is None or before.type != "gate" or before.status != "done":
            # The editor refuses this too, but a connector can turn publish after the save.
            raise PermissionError(
                f"'{stage.name}' calls a publish connector, so it runs only right after an answered"
                " gate (ADR 0007)"
            )
    call(run, stage_id, stage)
