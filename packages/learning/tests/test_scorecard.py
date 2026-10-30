"""The scorecard fold over run, verdict and outcome events."""

from typing import Any

from swarmify_kernel import Event
from swarmify_learning import scorecard
from swarmify_learning.scorecard import duration

T0, T1, T2 = "2026-10-26T10:00:00+00:00", "2026-10-26T10:05:00+00:00", "2026-10-26T10:07:30+00:00"


def ev(type: str, payload: dict[str, Any], run_id: str | None = None) -> Event:
    return Event.new(type, world_id="world_1", payload=payload, run_id=run_id)


def run(run_id: str, status: str, stages: list[dict], **over: Any) -> Event:
    payload = {
        "id": run_id,
        "name": "Short",
        "workflow_id": "wf_1",
        "workflow_version": 1,
        "baseline": False,
        "status": status,
        "created_at": over.pop("created_at", T0),
        "send_backs": 0,
        "spent_usd": 0.0,
        "stages": stages,
        **over,
    }
    return ev("run.updated", payload, run_id)


def stage(id: str, status: str, started: str | None, ended: str | None) -> dict:
    return {
        "id": id,
        "name": id,
        "type": "swarm",
        "status": status,
        "started_at": started,
        "ended_at": ended,
    }


def test_each_run_of_the_workflow_scores_first_try_send_backs_cost_duration_and_outcome() -> None:
    events = [
        run("run_1", "running", [stage("a", "running", T0, None)], created_at=T0),
        ev(
            "verdict.created",
            {"run_id": "run_1", "stage_id": "a", "loop": 1, "passed": False},
            "run_1",
        ),
        ev(
            "verdict.created",
            {"run_id": "run_1", "stage_id": "a", "loop": 2, "passed": True},
            "run_1",
        ),
        ev(
            "verdict.created",
            {"run_id": "run_1", "stage_id": "b", "loop": 1, "passed": True},
            "run_1",
        ),
        run(
            "run_1",
            "done",
            [stage("a", "done", T0, T1), stage("b", "done", T1, T2)],
            created_at=T0,
            send_backs=1,
            spent_usd=0.42,
        ),  # fmt: skip
        ev("outcome.recorded", {"path": "runs/run_1/x/artifacts/publish.md", "views": 10}, "run_1"),
        ev("outcome.recorded", {"path": "runs/run_1/x/artifacts/publish.md", "views": 25}, "run_1"),
        ev("outcome.recorded", {"path": "runs/run_1/y/artifacts/publish.md", "views": 5}, "run_1"),
        # a connector reporting clicks, ranked by its Primary Metric; views beside it do not count
        ev(
            "outcome.recorded",
            {
                "path": "runs/run_1/z/artifacts/publish.md",
                "metrics": {"clicks": 7, "views": 900},
                "primary": "clicks",
            },
            "run_1",
        ),
        run("run_2", "queued", [stage("a", "pending", None, None)], created_at=T1, baseline=True),
        run("run_3", "done", [stage("a", "done", T0, T1)], created_at=T2, workflow_id="wf_other"),
        run(
            "run_4",
            "done",
            [stage("a", "done", T0, T2)],
            created_at="2026-10-25T00:00:00+00:00",
            workflow_version=2,
        ),  # fmt: skip
    ]
    older, first, second = scorecard(events, "wf_1")
    assert [s.run_id for s in (older, first, second)] == ["run_4", "run_1", "run_2"]
    assert (first.judged, first.first_try, first.send_backs, first.cost_usd) == (2, 1, 1, 0.42)
    assert (first.status, first.duration_s, first.outcomes) == (
        "done",
        450.0,
        {"views": 30, "clicks": 7},  # the older events, written before outcomes, count as views
    )
    assert (second.baseline, second.judged, second.duration_s, second.outcomes) == (
        True,
        0,
        None,
        {},
    )
    assert (older.workflow_version, older.duration_s) == (2, 450.0)
    assert scorecard(events, "wf_nope") == []


def test_duration_is_none_until_every_stage_has_settled() -> None:
    assert duration([]) is None
    assert duration([stage("a", "pending", None, None)]) is None
    assert duration([stage("a", "done", T0, T1), stage("b", "waiting", T1, None)]) is None
    assert duration([stage("a", "done", T0, T1), stage("b", "skipped", None, None)]) == 300.0
    assert duration([stage("a", "failed", T0, T2), stage("b", "pending", None, None)]) == 450.0
