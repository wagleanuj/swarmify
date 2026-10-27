"""The retro stage at the runner's seam: a proposal raises a gate of kind skill that the inbox
lists and a decision answers; a retro with nothing to say just finishes the run."""

from pathlib import Path

import pytest

from runs_fakes import FakeNotifier, wait_for
from swarmify_runs import Illegal, Run, Runner, StageDef
from swarmify_runs.model import Run as RunModel

RESEARCH = StageDef("research", "connector", {"calls": "web_search"})
RETRO = StageDef("retro", "retro", {"kind": "skill", "ask": "Approve, edit or reject the skill."})


class FakeRetro:
    """Proposes for runs whose name says so, records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run: RunModel, stage_id: str, stage: StageDef) -> bool:
        self.calls.append((run.id, stage.name))
        return "proposes" in run.name


def status(runner: Runner, run_id: str) -> str:
    run = runner.get(run_id)
    assert run is not None
    return run.status


def test_a_proposal_waits_at_a_skill_gate_until_decided_and_a_quiet_retro_finishes(
    runner: Runner, notifier: FakeNotifier, tmp_path: Path
) -> None:
    retro = FakeRetro()
    runner.retro = retro
    run = runner.start("world_1", "wf_1", 1, "Short that proposes", [RESEARCH, RETRO])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    waiting = runner.get(run.id)
    assert waiting is not None and [s.status for s in waiting.stages] == ["done", "waiting"]
    (gate,) = runner.gates()
    assert (gate.run_id, gate.stage_name, gate.kind) == (run.id, "retro", "skill")
    assert gate.ask == "Approve, edit or reject the skill." and gate.earlier_stages == ["research"]
    opened = [e for e in notifier.events if e.type == "gate.opened"]
    assert len(opened) == 1 and opened[0].payload["kind"] == "skill"

    with pytest.raises(Illegal, match="deciding its proposal"):
        runner.decide(run.id, choice="approve")
    with pytest.raises(Illegal, match="skill gate cannot send back"):
        runner.decide(run.id, send_back_to="research", proposal=True)
    decided = runner.decide(run.id, choice="approve", note="Tightened it.", proposal=True)
    (decision,) = decided.decisions
    assert (decision.kind, decision.choice, decision.note) == ("skill", "approve", "Tightened it.")
    assert "Skill proposal approve." in Path(decision.path).read_text()
    wait_for(lambda: status(runner, run.id) == "done")
    assert retro.calls == [(run.id, "retro")]
    assert runner.gates() == []

    quiet: Run = runner.start("world_1", "wf_1", 1, "Short", [RESEARCH, RETRO])
    wait_for(lambda: status(runner, quiet.id) == "done")
    ended = runner.get(quiet.id)
    assert ended is not None and [s.status for s in ended.stages] == ["done", "done"]
    assert ended.decisions == [] and retro.calls[-1] == (quiet.id, "retro")


def test_a_retro_stage_without_a_retro_fails_honestly(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Short", [RETRO])
    wait_for(lambda: status(runner, run.id) == "failed")
    failed = runner.get(run.id)
    assert failed is not None and "retro stages cannot run yet" in (failed.stages[0].error or "")
