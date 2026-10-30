"""A gate answered from outside the web UI (a Remote Approval) and the `gate.closed` Notification
each closed gate sends, so the message a connector posted can say how it ended."""

from pathlib import Path

import pytest

from runs_fakes import FakeNotifier, wait_for
from swarmify_runs import Illegal, Runner, StageDef

WORK = StageDef("draft", "connector", {"calls": "writer"})
REVIEW = StageDef("review", "gate", {"ask": "Approve or send back.", "kind": "approve"})
PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})


def closed(notifier: FakeNotifier) -> list[dict]:
    return [e.payload for e in notifier.events if e.type == "gate.closed"]


def waiting(runner: Runner, run_id: str, stage: str, visit: int = 0) -> None:
    def check() -> bool:
        run = runner.get(run_id)
        assert run is not None
        at = next((s.name for s in run.stages if s.status == "waiting"), None)
        return run.status == "waiting_gate" and at == stage and run.send_backs == visit

    wait_for(check)


def test_a_remote_answer_names_its_gate_and_who_answered_and_the_close_says_so(
    runner: Runner, notifier: FakeNotifier
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Draft", [WORK, REVIEW, PICK])
    waiting(runner, run.id, "review")
    opened = next(e.payload for e in notifier.events if e.type == "gate.opened")
    assert (opened["stage_index"], opened["visit"]) == (1, 0)

    # A send-back from the web UI closes the gate's first visit; the message for it is stale.
    runner.decide(run.id, note="Shorter.", send_back_to="draft")
    waiting(runner, run.id, "review", visit=1)
    wait_for(lambda: len(closed(notifier)) == 1)
    assert closed(notifier)[0] == {
        "run_id": run.id,
        "name": "Draft",
        "stage_index": 1,
        "visit": 0,
        "stage": "review",
        "resolution": {
            "outcome": "sent_back",
            "choice": None,
            "send_back_to": "draft",
            "note": "Shorter.",
            "by": None,
        },
    }
    with pytest.raises(Illegal, match="already answered"):
        runner.decide(run.id, gate=(1, 0), by="anuj in Slack")

    answered = runner.decide(run.id, gate=(1, 1), by="anuj in Slack")
    assert answered.decisions[-1].by == "anuj in Slack"
    assert Path(answered.decisions[-1].path).read_text().endswith("Approved, by anuj in Slack.\n")
    waiting(runner, run.id, "pick", visit=1)
    wait_for(lambda: len(closed(notifier)) == 2)
    assert closed(notifier)[1]["resolution"]["by"] == "anuj in Slack"
    assert closed(notifier)[1]["resolution"]["outcome"] == "approved"
    with pytest.raises(Illegal, match="already answered"):  # the review message, clicked again
        runner.decide(run.id, gate=(1, 1), by="anuj in Slack")

    runner.abort(run.id)
    wait_for(lambda: len(closed(notifier)) == 3)
    assert closed(notifier)[2]["stage"] == "pick"
    assert closed(notifier)[2]["resolution"] == {"outcome": "aborted"}
    fetched = runner.get(run.id)
    assert fetched is not None and fetched.decisions[-1].by == "anuj in Slack"
