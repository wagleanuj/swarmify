"""Forks and promotes at the runner's seam: real DBOS on SQLite, a fake connector behind it."""

from pathlib import Path

import pytest

from runs_fakes import FakeConnector, wait_for
from swarmify_runs import Illegal, Pinned, Run, RunLink, Runner, StageDef

RESEARCH = StageDef("research", "connector", {"calls": "web_search"})
PICK = StageDef("pick", "gate", {"ask": "Pick one.", "kind": "pick"})
RENDER = StageDef("render", "connector", {"calls": "remotion_render", "input": "script.md"})
STAGES = [RESEARCH, PICK, RENDER]


def fetch(runner: Runner, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def at(runner: Runner, run_id: str, status: str) -> Run:
    wait_for(lambda: fetch(runner, run_id).status == status)
    return fetch(runner, run_id)


def called(connector: FakeConnector) -> list[str]:
    return [name for _, name in connector.calls]


def run_folder(tmp_path: Path, run: Run) -> Path:
    return tmp_path / "worlds" / run.world_id / "runs" / run.id


def finished_short(runner: Runner, tmp_path: Path) -> Run:
    """A done run with an artifact in its first stage (as Memory writes it) and a decision."""
    source = runner.start("world_a", "wf", 1, "Short", STAGES)
    at(runner, source.id, "waiting_gate")
    artifacts = run_folder(tmp_path, source) / source.stages[0].id / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "ideas.md").write_text("1. Hooks")
    chosen = f"runs/{source.id}/{source.stages[0].id}/artifacts/ideas.md"
    runner.decide(source.id, choice="idea 1", artifact=chosen)
    return at(runner, source.id, "done")


def test_a_fork_copies_the_stages_before_the_chosen_one_and_runs_on_from_there(
    runner: Runner, connector: FakeConnector, tmp_path: Path
) -> None:
    source = finished_short(runner, tmp_path)
    assert called(connector) == ["research", "render"]

    fork = runner.fork(source.id, 1)  # a different pick, without redoing the research
    assert fork.forked_from == RunLink(source.id, "world_a", "Short")
    assert fork.fork_stage_index == 1 and fork.promoted_from is None
    assert (fork.workflow_id, fork.workflow_version, fork.name) == ("wf", 1, "Short")
    assert [s.status for s in fork.stages] == ["done", "pending", "pending"]
    assert fork.stages[0].id != source.stages[0].id, "stage ids are connector idempotency keys"
    assert (fork.stages[0].started_at, fork.stages[0].ended_at) == (
        source.stages[0].started_at,
        source.stages[0].ended_at,
    )
    assert fork.decisions == [], "the pick runs again, so its decision is not copied"
    copied = run_folder(tmp_path, fork) / fork.stages[0].id / "artifacts" / "ideas.md"
    assert copied.read_text() == "1. Hooks"
    assert not (run_folder(tmp_path, fork) / fork.stages[1].id).exists()
    assert not (run_folder(tmp_path, fork) / source.stages[1].id).exists()

    at(runner, fork.id, "waiting_gate")
    assert called(connector) == ["research", "render"], "the copied stage did not run again"
    runner.decide(fork.id, choice="idea 3")
    at(runner, fork.id, "done")
    assert called(connector) == ["research", "render", "render"]
    assert fetch(runner, source.id).forks == [RunLink(fork.id, "world_a", "Short")]
    assert [r.id for r in runner.list_all("world_a")] == [fork.id, source.id]


def test_a_fork_after_a_gate_keeps_its_decision(
    runner: Runner, connector: FakeConnector, tmp_path: Path
) -> None:
    source = finished_short(runner, tmp_path)
    fork = runner.fork(source.id, 2)  # render again from the same pick
    (decision,) = fork.decisions
    assert (decision.stage_name, decision.choice) == ("pick", "idea 1")
    assert decision.id != source.decisions[0].id
    assert Path(decision.path) == run_folder(tmp_path, fork) / fork.stages[1].id / "decision.md"
    assert Path(decision.path).read_text() == Path(source.decisions[0].path).read_text()
    # The chosen artifact was copied too, so the record names the fork's own copy.
    assert decision.artifact == f"runs/{fork.id}/{fork.stages[0].id}/artifacts/ideas.md"
    assert (tmp_path / "worlds" / fork.world_id / str(decision.artifact)).is_file()
    at(runner, fork.id, "done")
    assert called(connector) == ["research", "render", "render"]
    assert fetch(runner, fork.id).stages[1].status == "done"


def test_a_fork_rechecks_its_workflow_version_and_starts_nothing_when_it_fails(
    runner: Runner, tmp_path: Path
) -> None:
    source = finished_short(runner, tmp_path)
    checked: list[tuple[str, int | None]] = []

    def pin(workflow_id: str, number: int | None = None) -> Pinned:
        checked.append((workflow_id, number))
        raise ValueError("render_video is not a connector of this world")

    runner.pin = pin
    with pytest.raises(ValueError, match="render_video"):
        runner.fork(source.id, 1)
    assert checked == [("wf", 1)]
    assert [r.id for r in runner.list_all("world_a")] == [source.id]


def test_a_fork_needs_every_earlier_stage_finished(runner: Runner) -> None:
    source = runner.start("world_a", "wf", 1, "Short", STAGES)
    at(runner, source.id, "waiting_gate")
    with pytest.raises(Illegal, match="pick has not finished"):
        runner.fork(source.id, 2)
    with pytest.raises(ValueError, match="not one of the run's 3 stages"):
        runner.fork(source.id, 3)
    with pytest.raises(KeyError):
        runner.fork("run_nope", 0)
    fork = runner.fork(source.id, 1)
    assert fork.stages[0].status == "done"
    at(runner, fork.id, "waiting_gate")


def test_a_promote_skips_the_earlier_stages_and_places_the_artifact_as_input(
    runner: Runner, connector: FakeConnector, tmp_path: Path
) -> None:
    old = tmp_path / "worlds/world_a/runs/run_old/stage_x/artifacts"
    old.mkdir(parents=True)
    (old / "script.md").write_text('---\ntype: "script"\nrun: "run_old"\n---\n\nA script.')
    pinned = Pinned("world_a", "wf", 3, "Short", STAGES)

    run = runner.promote(pinned, 2, old / "script.md", "runs/run_old/stage_x/artifacts/script.md")
    assert run.promoted_from == "runs/run_old/stage_x/artifacts/script.md"
    assert run.forked_from is None and run.workflow_version == 3
    assert [s.status for s in run.stages] == ["skipped", "skipped", "pending"]
    placed = run_folder(tmp_path, run) / run.stages[1].id / "artifacts" / "script.md"
    assert placed.read_text().endswith("A script.") and 'run: "run_old"' in placed.read_text()
    done = at(runner, run.id, "done")
    assert called(connector) == ["render"], "the skipped stages never ran"
    assert [s.status for s in done.stages] == ["skipped", "skipped", "done"]
    assert done.stages[0].started_at is None


def test_a_promote_places_the_artifact_under_the_stages_input_name(
    runner: Runner, tmp_path: Path
) -> None:
    old = tmp_path / "worlds/world_a/runs/run_old/stage_x/artifacts"
    old.mkdir(parents=True)
    (old / "script-the-chase.md").write_text('---\ntype: "script"\n---\n\nA script.')
    pinned = Pinned("world_a", "wf", 1, "Short", STAGES)
    rel = "runs/run_old/stage_x/artifacts/script-the-chase.md"
    run = runner.promote(pinned, 2, old / "script-the-chase.md", rel)
    folder = run_folder(tmp_path, run) / run.stages[1].id / "artifacts"
    assert [p.name for p in folder.iterdir()] == ["script.md"]
    assert (folder / "script.md").read_text().endswith("A script.")


def test_a_promoted_binary_keeps_its_sidecar_under_the_input_name(
    runner: Runner, tmp_path: Path
) -> None:
    old = tmp_path / "worlds/world_a/runs/run_old/stage_x/artifacts"
    old.mkdir(parents=True)
    (old / "clip-1.mp4").write_bytes(b"mp4")
    (old / "clip-1.mp4.md").write_text('---\ntype: "file"\n---\n\nSidecar.\n')
    upload = StageDef("upload", "connector", {"calls": "youtube_upload", "input": "clip.mp4"})
    pinned = Pinned("world_a", "wf", 1, "Short", [RESEARCH, upload])
    run = runner.promote(pinned, 1, old / "clip-1.mp4.md", "runs/run_old/stage_x/artifacts/x")
    folder = run_folder(tmp_path, run) / run.stages[0].id / "artifacts"
    assert sorted(p.name for p in folder.iterdir()) == ["clip.mp4", "clip.mp4.md"]
    assert (folder / "clip.mp4").read_bytes() == b"mp4"


def test_a_promoted_binary_brings_its_sidecar(runner: Runner, tmp_path: Path) -> None:
    old = tmp_path / "worlds/world_a/runs/run_old/stage_x/artifacts"
    old.mkdir(parents=True)
    (old / "clip.mp4").write_bytes(b"mp4")
    (old / "clip.mp4.md").write_text('---\ntype: "file"\n---\n\nSidecar of clip.mp4.\n')
    pinned = Pinned("world_a", "wf", 1, "Short", STAGES)
    rel = "runs/run_old/stage_x/artifacts/clip.mp4.md"
    run = runner.promote(pinned, 1, old / "clip.mp4.md", rel)
    folder = run_folder(tmp_path, run) / run.stages[0].id / "artifacts"
    assert (folder / "clip.mp4").read_bytes() == b"mp4" and (folder / "clip.mp4.md").exists()
    at(runner, run.id, "waiting_gate")


def test_a_promote_starts_after_the_first_stage_and_before_the_last(
    runner: Runner, tmp_path: Path
) -> None:
    (tmp_path / "script.md").write_text("A script.")
    pinned = Pinned("world_a", "wf", 1, "Short", STAGES)
    for index in (0, 3):
        with pytest.raises(ValueError, match="skips at least one stage"):
            runner.promote(pinned, index, tmp_path / "script.md", "script.md")
    with pytest.raises(ValueError, match="no artifact at missing.md"):
        runner.promote(pinned, 1, tmp_path / "missing.md", "missing.md")
    assert runner.list_all("world_a") == []
