"""Publish stages at the runner's seam (ADR 0007): the gate before one shows what will go out,
and the runner refuses to execute one that no answered gate precedes. Real DBOS on SQLite."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from runs_fakes import FakeConnector, FakeNotifier, wait_for
from swarmify_runs import PublishPreview, Runner, SqliteStorage, StageDef

CONFIRM = StageDef("confirm", "gate", {"ask": "Post it?", "kind": "confirm"})
PUBLISH = StageDef("publish", "connector", {"calls": "youtube", "params": {"title": "Hooks"}})
RENDER = StageDef("render", "connector", {"calls": "remotion_render"})


def previews(_world: str, stage: StageDef) -> PublishPreview | None:
    """Stands in for the Connectors registry: youtube publishes, nothing else does."""
    if stage.config.get("calls") != "youtube":
        return None
    title = stage.config["params"]["title"]
    return PublishPreview("youtube", title, "public", "2099-01-01T09:00:00Z")


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
    opened = next(e for e in notifier.events if e.type == "gate.opened")
    assert opened.payload["publish"]["title"] == "Hooks"
    assert opened.payload["publish"]["schedule"] == "2099-01-01T09:00:00Z"
    assert connector.calls == []

    runner.decide(run.id)
    wait_for(lambda: status(runner, run.id) == "done")
    assert [name for _, name in connector.calls] == ["publish"]


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
