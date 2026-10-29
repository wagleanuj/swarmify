"""`swarmify-eval`: run the weekly short as baseline and as swarm over the eval task set, against
a running swarmify API (with the real model behind it), and store the report in the eval world.

Each trial gets a fresh world, its own copy of the workflow and one run; the outputs it left
(`ideas.md`, `script.md`) are graded, never the path it took, and each pair of trials (one an
arm, same task and number) goes to the API's blind judge, which picks the better pair. A retro
proposal is rejected so the run can end, a budget gate or the timeout aborts the trial, which
then fails.

    uv run swarmify-eval --k 3                  # every task, 3 trials an arm
    uv run swarmify-eval --tasks 2 --k 1        # a smoke run

`SWARMIFY_PASSWORD` (and `SWARMIFY_USER`) are sent as basic auth when set.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx2 as httpx

from swarmify_learning.evals import ARMS, TASKS, EvalTask, Trial, grade, workflow_yaml
from swarmify_learning.scorecard import duration

ENDED = ("done", "failed", "aborted")


@dataclass(frozen=True)
class Settings:
    budget_usd: float = 5.0
    """Each trial's Budget; a trial that spends it is aborted."""
    timeout_s: float = 45 * 60
    poll_s: float = 5.0


def _ok(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


def _settle(http: httpx.Client, run_id: str) -> None:
    """A run at a gate: reject a retro proposal (the trial world is thrown away), abort anything
    else, which is a budget gate since the eval workflow has no gate of its own."""
    proposal = http.get(f"/api/runs/{run_id}/proposal")
    if proposal.status_code == 200:
        body = {"approve": False, "note": "eval trial"}
        http.post(f"/api/proposals/{proposal.json()['id']}/decide", json=body)
    else:
        http.post(f"/api/runs/{run_id}/abort")


def trial(
    http: httpx.Client, world_id: str, task: EvalTask, arm: str, index: int, s: Settings
) -> Trial:
    yaml = workflow_yaml(task, s.budget_usd)
    workflow = _ok(http.post(f"/api/worlds/{world_id}/workflows", json={"yaml": yaml}))
    start = {"baseline": arm == "baseline"}
    run = _ok(http.post(f"/api/workflows/{workflow['workflow_id']}/runs", json=start))
    deadline = time.monotonic() + s.timeout_s
    timed_out = False
    while run["status"] not in ENDED:
        if time.monotonic() > deadline:
            if timed_out:
                break  # the abort did not take either; the trial fails as the run stands
            timed_out, deadline = True, time.monotonic() + s.timeout_s  # grace for the abort
            http.post(f"/api/runs/{run['id']}/abort")
        elif run["status"] == "waiting_gate":
            _settle(http, run["id"])
        time.sleep(s.poll_s)
        run = _ok(http.get(f"/api/runs/{run['id']}"))
    library = _ok(http.get(f"/api/worlds/{world_id}/library"))
    outputs = {i["name"]: i["body"] for i in library if i["run_id"] == run["id"]}
    failed = grade(task, outputs) if run["status"] == "done" else [f"the run {run['status']}"]
    if timed_out:
        failed.insert(0, f"timed out after {s.timeout_s:.0f} s")
    work = [st for st in run["stages"] if st["type"] != "retro"]  # the retro is not the work
    return Trial(
        task.id,
        arm,
        index,
        world_id,
        run["id"],
        run["status"],
        float(run["spent_usd"]),
        duration(work),
        failed,
    )


def _safe(http: httpx.Client, task: EvalTask, arm: str, index: int, s: Settings) -> Trial:
    """A trial that broke on the API's side fails, it does not end the eval. Its world id is
    kept so `clean` still removes it."""
    world_id = ""
    try:
        name = f"Eval {task.id} {arm} {index + 1}"
        world_id = _ok(http.post("/api/worlds", json={"name": name}))["id"]
        return trial(http, world_id, task, arm, index, s)
    except httpx.HTTPError as exc:
        return Trial(task.id, arm, index, world_id, "", "error", 0.0, None, [f"error: {exc}"])


def _judge(http: httpx.Client, base: Trial, swarm: Trial) -> dict[str, Any] | None:
    """The blind judge's pick for the pair, or None when a side never got a run or the judge
    failed: that pair is left undecided rather than ending the eval."""
    if not (base.run_id and swarm.run_id):
        return None
    pair = {
        "task_id": base.task_id,
        "index": base.index,
        "baseline": {"world_id": base.world_id, "run_id": base.run_id},
        "swarm": {"world_id": swarm.world_id, "run_id": swarm.run_id},
    }
    try:
        return _ok(http.post("/api/evals/judge", json=pair))
    except httpx.HTTPError:
        return None


def run_eval(
    http: httpx.Client,
    tasks: list[EvalTask],
    k: int,
    settings: Settings | None = None,
    clean: bool = False,
    say: Callable[[str], None] = lambda _line: None,
) -> dict[str, Any]:
    """Every task k times on each arm, each pair judged blind, then the stored report. `clean`
    deletes trial worlds."""
    s = settings or Settings()
    jobs = [(t, arm, i) for t in tasks for i in range(k) for arm in ARMS]

    trials, judgements = [], []
    for job in jobs:
        done = _safe(http, *job, s)
        verdict = "accepted" if done.passed else "; ".join(done.failed)
        say(f"{done.task_id} {done.arm} {done.index + 1}: {verdict}")
        trials.append(done)
        if done.arm == ARMS[-1]:  # the pair is complete: baseline then swarm
            pick = _judge(http, trials[-2], done)
            judged = "undecided" if pick is None else f"{pick['winner'] or 'tie'}: {pick['reason']}"
            say(f"{done.task_id} {done.index + 1} judged: {judged}")
            if pick is not None:
                judgements.append(pick)
    body = {"k": k, "trials": [asdict(t) for t in trials], "judgements": judgements}
    report = _ok(http.post("/api/evals", json=body))
    if clean:
        for t in trials:
            if t.world_id:
                http.delete(f"/api/worlds/{t.world_id}")
    return report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="swarmify-eval", description="Run the eval: the weekly short as baseline and as swarm."
    )
    p.add_argument("--api", default=os.environ.get("SWARMIFY_BASE_URL", "http://localhost:8000"))
    p.add_argument("--k", type=int, default=3, help="trials an arm per task (default 3)")
    p.add_argument("--tasks", type=int, default=len(TASKS), help="the first N tasks (all)")
    p.add_argument("--budget", type=float, default=Settings.budget_usd, help="USD a trial")
    p.add_argument("--timeout", type=float, default=Settings.timeout_s, help="seconds a trial")
    p.add_argument("--clean", action="store_true", help="delete trial worlds afterwards")
    a = p.parse_args(argv)
    password = os.environ.get("SWARMIFY_PASSWORD")
    auth = (os.environ.get("SWARMIFY_USER", "swarmify"), password) if password else None
    # a judge session answers within minutes; every other call within seconds
    timeout = httpx.Timeout(60, read=600)
    with httpx.Client(base_url=a.api, auth=auth, timeout=timeout) as http:
        report = run_eval(
            http,
            TASKS[: a.tasks],
            a.k,
            Settings(a.budget, a.timeout),
            a.clean,
            say=lambda line: print(line, file=sys.stderr),
        )
    print(report["verdict"])
    print(f"Stored in the {report['id']} report of the Evals world; see /evals.")


if __name__ == "__main__":
    main()
