"""Crash recovery: kill the process mid-stage, start a new one, and the run resumes at the
last completed stage. A completed step never runs again."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from runs_fakes import FakeConnector, wait_for
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_runs import Runner, SqliteStorage, StageDef

CHILD = Path(__file__).parent / "crash_child.py"


def child(root: Path, mode: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(CHILD.parent)}
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(root), mode], stdout=subprocess.PIPE, text=True, env=env
    )


def test_killed_mid_stage_the_run_resumes_without_repeating_finished_stages(
    tmp_path: Path,
) -> None:
    log = tmp_path / "calls.log"
    first = child(tmp_path, "start")
    try:
        wait_for(lambda: log.exists() and log.read_text().splitlines() == ["a", "slow"], 30)
    finally:
        first.send_signal(signal.SIGKILL)
        first.wait()

    storage = SqliteStorage(tmp_path / "swarmify.db")
    (run,) = storage.list_runs("world_1")
    assert run.status == "running"
    assert [s.status for s in run.stages] == ["done", "running", "pending"]

    second = child(tmp_path, "resume")
    out, _ = second.communicate(timeout=60)
    assert second.returncode == 0

    # `a` finished before the kill and is not called again; `slow` never finished, so it reruns.
    assert log.read_text().splitlines() == ["a", "slow", "slow"]
    steps = json.loads(out.strip().splitlines()[-1])
    assert steps.count("swarmify.execute") == 2  # one per connector stage, none duplicated
    done = storage.get_run(run.id)
    assert done is not None and done.status == "done"
    with closing(connect(storage.path)) as conn:
        snapshots = [
            [s["status"] for s in e.payload["stages"]]
            for e in read_after(conn, "world_1", 0)
            if e.type == "run.updated"
        ]
    assert snapshots.count(["done", "running", "pending"]) == 1


def test_a_run_waiting_at_its_gate_survives_a_restart(
    tmp_path: Path, storage: SqliteStorage, connector: FakeConnector
) -> None:
    stages = [StageDef("render", "connector"), StageDef("pick", "gate")]
    first = Runner(storage, tmp_path / "dbos.sqlite", connector, tmp_path, poll_seconds=0.05)
    first.launch()
    run = first.start("world_1", "wf_1", 1, "Restart", stages)
    wait_for(lambda: getattr(storage.get_run(run.id), "status", None) == "waiting_gate")
    first.shutdown()

    second = Runner(storage, tmp_path / "dbos.sqlite", connector, tmp_path, poll_seconds=0.05)
    second.launch()
    try:
        second.decide(run.id)
        wait_for(lambda: getattr(storage.get_run(run.id), "status", None) == "done")
    finally:
        second.shutdown()
    assert [name for _, name in connector.calls] == ["render"]


def test_a_queued_run_left_behind_is_enqueued_on_launch(
    tmp_path: Path, storage: SqliteStorage, connector: FakeConnector
) -> None:
    from swarmify_runs import Run

    stub = Run("run_left", "world_1", "wf_1", 1, "Left", "queued", "t", "t", [])
    storage.add_run(stub, [StageDef("render", "connector")])
    runner = Runner(storage, tmp_path / "dbos.sqlite", connector, tmp_path, poll_seconds=0.05)
    runner.launch()
    try:
        wait_for(lambda: getattr(storage.get_run("run_left"), "status", None) == "done")
    finally:
        runner.shutdown()


def test_a_run_reaching_its_gate_while_shutdown_runs_does_not_outlive_it(
    tmp_path: Path, storage: SqliteStorage
) -> None:
    """Shutdown wakes the runs waiting when it starts. One that reaches its gate a moment later
    must stop too, or its thread waits an hour and the process cannot exit."""
    release = threading.Event()

    def status() -> str | None:
        return getattr(storage.get_run(run.id), "status", None)

    class Late(Runner):
        def close(self) -> None:  # shutdown has woken the waiting runs; now this one waits
            release.set()
            wait_for(lambda: status() == "waiting_gate")
            time.sleep(0.2)
            super().close()

    def render(*_: object) -> None:
        release.wait(10)

    stages = [StageDef("render", "connector"), StageDef("pick", "gate")]
    late = Late(storage, tmp_path / "dbos.sqlite", render, tmp_path)
    late.launch()
    run = late.start("world_1", "wf_1", 1, "Late", stages)
    wait_for(lambda: status() == "running")
    late.shutdown()
    wait_for(lambda: not [t for t in threading.enumerate() if t.name.startswith("dbos-executor")])
