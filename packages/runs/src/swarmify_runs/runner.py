"""The DBOS shell around the stage state machine (ADR 0014).

One DBOS workflow per run, id equal to the run id, enqueued on a queue partitioned by world so
each world runs at most `cap` runs at once. Every side effect is a DBOS step: loading the run,
saving each new state, executing a stage. A crash resumes from the last completed step, so a
finished stage never runs twice. DBOS is a process singleton, hence one module-level Runner.

The workflow is the only writer of run state. People act through RunControl, whose messages
(decide, pause, resume, abort) arrive on the run's control topic: at a gate or while paused the
workflow waits for them; around a stage it drains what arrived meanwhile.
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig, SetEnqueueOptions, SetWorkflowID

from swarmify_kernel import Event, new_id
from swarmify_runs.control import CONTROL_TOPIC, RunControl, ended_event
from swarmify_runs.machine import (
    TERMINAL,
    Aborted,
    GateAnswered,
    Illegal,
    OpenGate,
    Paused,
    Resumed,
    RunState,
    SentBack,
    StageFailed,
    StageFinished,
    Started,
    transition,
)
from swarmify_runs.machine import (
    Event as MachineEvent,
)
from swarmify_runs.model import Run, StageDef
from swarmify_runs.ports import Notifier, Storage

QUEUE = "runs"
DEFAULT_CAP = 2
# ponytail: pinned so a code change does not orphan runs in flight (DBOS only recovers
# workflows of the current application version). Bump it when the workflow's step order changes.
APP_VERSION = "2"

Connector = Callable[[Run, str, StageDef], None]
"""Calls one connector stage. Arguments: the run, the stage id (the idempotency key) and the
stage's definition."""
Swarm = Callable[[Run, str, StageDef], None]
"""Runs one swarm stage to its end, with the same arguments. Raises to fail the stage."""

_runner: "Runner | None" = None
# Runs whose workflow thread sits in recv. Shutdown wakes them, because DBOS cannot interrupt a
# recv and Python joins executor threads at exit.
_waiting: set[str] = set()
_waiting_lock = threading.Lock()
_stopping = threading.Event()
WAKE = {"swarmify": "wake"}


class LogNotifier:
    """The adapter until Slack ships (T16): one log line per notification."""

    log = logging.getLogger("swarmify.notify")

    def notify(self, event: Event) -> None:
        self.log.info("%s %s", event.type, event.payload)


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
        swarm: Swarm | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        folder, notify = data_dir or system_db.parent, notifier or LogNotifier()
        super().__init__(storage, system_db, folder, notify)
        self.connector = connector
        self.swarm = swarm
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
        with SetWorkflowID(run.id), SetEnqueueOptions(queue_partition_key=run.world_id):
            DBOS.enqueue_workflow(QUEUE, execute_run, run.id)


def _current() -> Runner:
    assert _runner is not None, "Runner.launch() was not called"
    return _runner


@DBOS.workflow(name="swarmify.run")
def execute_run(run_id: str) -> str:
    kinds, status, stages, send_backs = _load(run_id)
    state = RunState(status, tuple(stages), send_backs)  # type: ignore[arg-type]  # CHECKed rows
    events: list[MachineEvent] = [*_drain(run_id), Started()]
    error: str | None = None
    while True:
        event, events = events[0], events[1:]
        try:
            state, commands = transition(tuple(kinds), state, event)
        except Illegal:
            continue  # a stale message, e.g. a resume for a run that is not paused
        _save(run_id, state, error, bool(commands and isinstance(commands[0], OpenGate)))
        if state.status in TERMINAL:
            return state.status
        if events:
            continue
        if not commands or isinstance(commands[0], OpenGate):
            events = [_as_event(_wait(run_id))]
            continue
        index, error = commands[0].index, None
        try:
            _execute(run_id, index)
            outcome: MachineEvent = StageFinished(index)
        except Exception as exc:  # a failed stage fails the run; the error lands on the stage
            outcome, error = StageFailed(index), f"{type(exc).__name__}: {exc}"
        # ponytail: a pause or abort sent mid-stage lands once the stage finishes. Cancelling a
        # stage midway arrives with long swarm stages (T07).
        events = [*_drain(run_id), outcome]


def _as_event(message: dict[str, Any]) -> MachineEvent:
    match message["action"]:
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


def _drain(run_id: str) -> list[MachineEvent]:
    """Every control message that arrived while the workflow was busy, in order."""
    events: list[MachineEvent] = []
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
def _load(run_id: str) -> tuple[list[str], str, list[str], int]:
    run = _current().storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    return [s.type for s in run.stages], run.status, [s.status for s in run.stages], run.send_backs


@DBOS.step(name="swarmify.save")
def _save(run_id: str, state: RunState, error: str | None, gate_opened: bool) -> None:
    """Save the state; when a gate opened or the run ended, record the event and notify."""
    runner = _current()
    run = runner.storage.save_state(run_id, state, error)
    if run is None:
        return
    if gate_opened:
        index = state.stages.index("waiting")
        _, stage = runner.storage.stage_def(run_id, index)
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
            },
        )
    elif state.status in TERMINAL:
        event = ended_event(run)
    else:
        return
    runner.storage.record(event)
    runner.notifier.notify(event)


@DBOS.step(name="swarmify.execute")
def _execute(run_id: str, index: int) -> None:
    runner = _current()
    stage_id, stage = runner.storage.stage_def(run_id, index)
    call = {"connector": runner.connector, "swarm": runner.swarm}.get(stage.type)
    if call is None:
        # ponytail: workflow stages arrive with child runs (T14).
        raise NotImplementedError(f"{stage.type} stages cannot run yet")
    run = runner.storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    call(run, stage_id, stage)
