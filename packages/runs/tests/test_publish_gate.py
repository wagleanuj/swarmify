"""Publish stages at the runner's seam (ADR 0007): the gate before one shows what will go out,
and the runner refuses to execute one that no answered gate precedes. Real DBOS on SQLite."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import Illegal, PublishPreview, Run, Runner, SqliteStorage, StageDef

CONFIRM = StageDef("confirm", "gate", {"ask": "Post it?", "kind": "confirm"})
PUBLISH = StageDef("publish", "connector", {"calls": "youtube", "params": {"title": "Hooks"}})
RENDER = StageDef("render", "connector", {"calls": "remotion_render"})
RESOLVED = {"schedule": "2099-01-01T09:00:00Z"}
"""What the stage's relative schedule resolves to now; a test moves the clock by changing it."""


def previews(_run: Run, stage: StageDef, edits: dict[str, Any]) -> PublishPreview | None:
    """Stands in for the Connectors registry: youtube publishes, nothing else does."""
    if stage.config.get("calls") != "youtube":
        return None
    params = {**stage.config["params"], **edits}
    title, description = params["title"], params.get("description", "")
    problems = [] if title else ["a publish stage needs params.title"]
    return PublishPreview(
        "youtube", title, "public", RESOLVED["schedule"], description, problems=problems
    )


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, connector: FakeConnector, notifier: FakeNotifier
) -> Iterator[Runner]:
    runner = Runner(
        storage,
        tmp_path / "dbos.sqlite",
        connector,
        tmp_path,
        notifier,
        poll_seconds=0.05,
        previews=previews,
    )
    runner.launch()
    yield runner
    runner.shutdown()


def status(runner: Runner, run_id: str) -> str:
    run = runner.get(run_id)
    assert run is not None
    return run.status


def test_the_gate_before_a_publish_stage_shows_what_goes_out(
    runner: Runner, connector: FakeConnector, notifier: FakeNotifier
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Weekly short", [CONFIRM, PUBLISH])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    (gate,) = runner.gates()
    assert gate.publish == PublishPreview("youtube", "Hooks", "public", "2099-01-01T09:00:00Z")
    wait_for(lambda: any(e.type == "gate.opened" for e in notifier.events))  # notify follows save
    opened = next(e for e in notifier.events if e.type == "gate.opened")
    assert opened.payload["publish"]["title"] == "Hooks"
    assert opened.payload["publish"]["schedule"] == "2099-01-01T09:00:00Z"
    assert connector.calls == []

    runner.decide(run.id)
    wait_for(lambda: status(runner, run.id) == "done")
    assert [name for _, name in connector.calls] == ["publish"]


def test_the_confirmed_title_and_description_are_recorded_with_the_decision(
    runner: Runner, connector: FakeConnector
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Weekly short", [CONFIRM, PUBLISH])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    with pytest.raises(ValueError, match="needs params.title"):
        runner.decide(run.id, title="")

    decided = runner.decide(run.id, title="Edited", description="Tonight")
    (decision,) = decided.decisions
    shown = {"title": "Edited", "description": "Tonight", "schedule": "2099-01-01T09:00:00Z"}
    assert decision.publish == shown
    assert '"title": "Edited"' in Path(decision.path).read_text()
    wait_for(lambda: status(runner, run.id) == "done")
    assert runner.get(run.id).decisions[0].publish == shown  # type: ignore[union-attr]


def test_a_schedule_that_moved_since_the_gate_was_shown_is_refused(
    runner: Runner, connector: FakeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Weekly short", [CONFIRM, PUBLISH])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    (gate,) = runner.gates()
    assert gate.publish is not None
    shown = gate.publish.schedule
    # "09:00" shown at 08:58 as today; confirmed at 09:01 it resolves to tomorrow.
    monkeypatch.setitem(RESOLVED, "schedule", "2099-01-02T09:00:00Z")
    with pytest.raises(ValueError, match="schedule changed"):
        runner.decide(run.id, title="Hooks", schedule=shown)
    assert status(runner, run.id) == "waiting_gate"
    assert runner.get(run.id).decisions == []  # type: ignore[union-attr]

    decided = runner.decide(run.id, title="Hooks", schedule="2099-01-02T09:00:00Z")
    assert decided.decisions[0].publish["schedule"] == "2099-01-02T09:00:00Z"  # type: ignore[index]
    wait_for(lambda: status(runner, run.id) == "done")
    assert [name for _, name in connector.calls] == ["publish"]


def test_only_a_gate_before_a_publish_stage_takes_a_title(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Render", [CONFIRM, RENDER])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    with pytest.raises(ValueError, match="only a gate before a publish stage"):
        runner.decide(run.id, title="t")


def test_a_gate_before_a_stage_that_does_not_publish_has_no_preview(runner: Runner) -> None:
    run = runner.start("world_1", "wf_1", 1, "Render", [CONFIRM, RENDER])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    assert runner.gates()[0].publish is None


@pytest.mark.parametrize(
    "stages", [[PUBLISH], [RENDER, PUBLISH]], ids=["first stage", "after a connector"]
)
def test_the_runner_refuses_a_publish_stage_no_gate_precedes(
    runner: Runner, connector: FakeConnector, stages: list[StageDef]
) -> None:
    run = runner.start("world_1", "wf_1", 1, "Sneaky", stages)
    wait_for(lambda: status(runner, run.id) == "failed")
    failed = runner.get(run.id)
    assert failed is not None
    error = failed.stages[-1].error or ""
    assert "calls a publish connector, so it runs only right after an answered gate" in error
    assert "publish" not in [name for _, name in connector.calls]


@pytest.mark.parametrize("kind", ["approve", "confirm"])
def test_a_gate_before_a_broken_publish_stage_refuses_to_go_on(
    runner: Runner, connector: FakeConnector, kind: str
) -> None:
    gate = StageDef("check", "gate", {"ask": "Good?", "kind": kind})
    broken = StageDef("publish", "connector", {"calls": "youtube", "params": {"title": ""}})
    run = runner.start("world_1", "wf_1", 1, "Weekly short", [RENDER, gate, broken])
    wait_for(lambda: status(runner, run.id) == "waiting_gate")
    with pytest.raises(Illegal, match="needs params.title"):
        runner.decide(run.id)
    assert status(runner, run.id) == "waiting_gate"
    assert runner.get(run.id).decisions == []  # type: ignore[union-attr]
    if kind == "approve":  # sending back still works: the earlier stage can fix it
        runner.decide(run.id, send_back_to="render")
        wait_for(lambda: status(runner, run.id) == "waiting_gate")
    assert "publish" not in [name for _, name in connector.calls]
