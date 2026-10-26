"""A swarmify runner process for the crash test. `python crash_child.py <root> start|resume`.

start: launches, starts a run of [a, slow, pick], prints its id; the `slow` connector hangs so
the parent can kill this process mid-stage. resume: launches (DBOS recovers the run), answers
the gate once the run waits there, waits for done, prints the run's DBOS step names, exits.
"""

import json
import sys
import time
from pathlib import Path

from runs_fakes import migrated, wait_for
from swarmify_runs import Runner, SqliteStorage, StageDef

root, mode = Path(sys.argv[1]), sys.argv[2]
log = root / "calls.log"


def connector(stage_id: str, stage: StageDef) -> None:
    with log.open("a") as f:
        f.write(f"{stage.name}\n")
    if stage.name == "slow" and mode == "start":
        time.sleep(3600)


storage = SqliteStorage(migrated(root))
runner = Runner(storage, root / "dbos.sqlite", connector, poll_seconds=0.05)
runner.launch()
if mode == "start":
    stages = [StageDef("a", "connector"), StageDef("slow", "connector"), StageDef("pick", "gate")]
    run = runner.start("world_1", "wf_1", 1, "Crash", stages)
    print(run.id, flush=True)
    time.sleep(3600)
else:
    from dbos import DBOS

    (run_id,) = [r.id for r in storage.list_runs("world_1")]
    wait_for(lambda: storage.get_run(run_id).status == "waiting_gate")  # type: ignore[union-attr]
    runner.answer_gate(run_id, {"ok": True})
    wait_for(lambda: storage.get_run(run_id).status == "done")  # type: ignore[union-attr]
    print(json.dumps([s["function_name"] for s in DBOS.list_workflow_steps(run_id)]), flush=True)
    runner.shutdown()
