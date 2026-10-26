"""The DBOS shell around the stage state machine (ADR 0014).

One DBOS workflow per run, id equal to the run id, enqueued on a queue partitioned by world so
each world runs at most `cap` runs at once. Every side effect is a DBOS step: loading the run,
saving each new state, executing a stage. A crash resumes from the last completed step, so a
finished stage never runs twice. DBOS is a process singleton, hence one module-level Runner.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig, SetEnqueueOptions, SetWorkflowID

from swarmify_kernel import new_id
from swarmify_runs.machine import (
    TERMINAL,
    Event,
    GateAnswered,
    OpenGate,
    RunState,
    StageFailed,
    StageFinished,
    Started,
    transition,
)
from swarmify_runs.model import Run, StageDef
from swarmify_runs.ports import Storage

QUEUE = "runs"
GATE_TOPIC = "gate"
DEFAULT_CAP = 2
# ponytail: pinned so a code change does not orphan runs in flight (DBOS only recovers
# workflows of the current application version). Bump it when the workflow's step order changes.
APP_VERSION = "1"

Connector = Callable[[Run, str, StageDef], None]
"""Calls one connector stage. Arguments: the run, the stage id (the idempotency key) and the
stage's definition."""
Swarm = Callable[[Run, str, StageDef], None]
"""Runs one swarm stage to its end, with the same arguments. Raises to fail the stage."""

_runner: "Runner | None" = None
# Runs whose workflow thread sits in recv at a gate. Shutdown wakes them, because DBOS cannot
# interrupt a recv and Python joins executor threads at exit.
_waiting: set[str] = set()
_waiting_lock = threading.Lock()
_stopping = threading.Event()
WAKE = {"swarmify": "wake"}


class _Shutdown(BaseException):
    """Leaves a workflow PENDING so the next launch recovers it. DBOS records a workflow's
    Exception as its outcome, but lets a BaseException through unrecorded, like a crash."""


class Runner:
    def __init__(
        self,
        storage: Storage,
        system_db: Path,
        connector: Connector,
        cap: int = DEFAULT_CAP,
        swarm: Swarm | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.storage = storage
        self.system_db = system_db
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
        """Release the gate waiters, then stop DBOS. Interrupted runs resume on the next launch."""
        global _runner
        _stopping.set()
        with _waiting_lock:
            waiting = list(_waiting)
        for run_id in waiting:
            DBOS.send(run_id, WAKE, topic=GATE_TOPIC)
        deadline = time.monotonic() + seconds
        while _waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        DBOS.destroy()
        _runner = None

    def start(
        self,
        world_id: str,
        workflow_id: str,
        version: int,
        name: str,
        stages: list[StageDef],
    ) -> Run:
        """Record a queued run pinned to `version` and enqueue it."""
        now = datetime.now(UTC).isoformat()
        run = Run(new_id("run"), world_id, workflow_id, version, name, "queued", now, now, [])
        run = self.storage.add_run(run, stages)
        self._enqueue(run)
        return run

    def answer_gate(self, run_id: str, decision: dict[str, Any]) -> None:
        """Deliver a person's decision to the run waiting at its gate."""
        DBOS.send(run_id, decision, topic=GATE_TOPIC)

    def get(self, run_id: str) -> Run | None:
        return self.storage.get_run(run_id)

    def list_all(self, world_id: str) -> list[Run]:
        return self.storage.list_runs(world_id)

    def delete_world(self, world_id: str) -> None:
        """Cancel the world's unfinished runs, then drop its rows."""
        for run_id in self.storage.active_run_ids(world_id):
            DBOS.cancel_workflow(run_id)
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
    kinds, status, stages = _load(run_id)
    state = RunState(status, tuple(stages))  # type: ignore[arg-type]  # read from CHECKed rows
    event: Event = Started()
    error: str | None = None
    while True:
        state, commands = transition(tuple(kinds), state, event)
        _save(run_id, state.status, list(state.stages), error)
        if not commands or state.status in TERMINAL:
            return state.status
        command, error = commands[0], None
        if isinstance(command, OpenGate):
            _wait_at_gate(run_id)
            event = GateAnswered(command.index)
            continue
        try:
            _execute(run_id, command.index)
            event = StageFinished(command.index)
        except Exception as exc:  # a failed stage fails the run; the error lands on the stage
            event, error = StageFailed(command.index), f"{type(exc).__name__}: {exc}"


def _wait_at_gate(run_id: str) -> Any:
    with _waiting_lock:
        _waiting.add(run_id)
    try:
        while True:
            message = DBOS.recv(GATE_TOPIC, timeout_seconds=3600)
            if message == WAKE and _stopping.is_set():
                raise _Shutdown(run_id)
            if message is not None and message != WAKE:
                return message
    finally:
        with _waiting_lock:
            _waiting.discard(run_id)


@DBOS.step(name="swarmify.load")
def _load(run_id: str) -> tuple[list[str], str, list[str]]:
    run = _current().storage.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    return [s.type for s in run.stages], run.status, [s.status for s in run.stages]


@DBOS.step(name="swarmify.save")
def _save(run_id: str, status: str, stages: list[str], error: str | None) -> None:
    _current().storage.save_state(run_id, RunState(status, tuple(stages)), error)  # type: ignore[arg-type]


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
