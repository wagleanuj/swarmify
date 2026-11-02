"""Artifact Edit at the RunControl seam (ADR 0027): a text file a finished stage made is
rewritten, its earlier content kept, the stages that already read it named, and the stages
still to come read the new text. Real DBOS on SQLite, fakes for the stage calls that write and
read files in the run folder."""

import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from runs_fakes import FakeNotifier, wait_for
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect
from swarmify_runs import (
    Illegal,
    Run,
    RunChange,
    Runner,
    SqliteStorage,
    StageDef,
    run_folder,
    stage_folder,
)

WRITE = StageDef("write", "swarm", {"goal": "Write the metadata.", "done_when": "It is there."})
TAG = StageDef("tag", "swarm", {"goal": "Tag it.", "done_when": "Tagged."})
CONFIRM = StageDef("confirm", "gate", {"ask": "Go on?", "kind": "approve"})
UPLOAD = StageDef("upload", "connector", {"calls": "youtube", "input": "metadata.md"})

METADATA = "---\ntype: metadata\n---\n\n#one #two #three #four #five #six #seven\n"
FIVE = "---\ntype: metadata\n---\n\n#one #two #three #four #five\n"


class Files:
    """Stands in for both stage calls: `write` leaves metadata.md and a video in its
    artifacts folder, `remake` a newer metadata.md; every other stage reads the newest
    metadata.md of the run, as a connector's `input` does, once `hold` is set."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.read: dict[str, str] = {}
        self.hold = threading.Event()
        self.hold.set()

    def __call__(self, run: Run, stage_id: str, stage: StageDef) -> None:
        if stage.name == "write":
            folder = run_folder(self.data_dir, run) / stage_id / "artifacts"
            folder.mkdir(parents=True)
            (folder / "metadata.md").write_text(METADATA)
            (folder / "short.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42\xff\xfe")
            return
        self.hold.wait(10)
        if stage.name == "remake":
            folder = run_folder(self.data_dir, run) / stage_id / "artifacts"
            folder.mkdir(parents=True)
            (folder / "metadata.md").write_text(FIVE)
            return
        found = run_folder(self.data_dir, run).rglob("metadata.md")
        self.read[stage.name] = max(found, key=lambda p: p.stat().st_mtime).read_text()


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


@pytest.fixture
def runner(
    tmp_path: Path, storage: SqliteStorage, notifier: FakeNotifier, files: Files
) -> Iterator[Runner]:
    runner = Runner(
        storage, tmp_path / "dbos.sqlite", files, tmp_path, notifier, swarm=files,
        poll_seconds=0.05,
    )  # fmt: skip
    runner.launch()
    yield runner
    runner.shutdown()


def at_gate(runner: Runner, stages: list[StageDef]) -> Run:
    run = runner.start("world_1", "wf_1", 1, "Short", stages)
    wait_for(lambda: fetch(runner, run.id).status == "waiting_gate")
    return fetch(runner, run.id)


def fetch(runner: Runner, run_id: str) -> Run:
    run = runner.get(run_id)
    assert run is not None
    return run


def artifact_edit(run: Run, path: str, text: str) -> RunChange:
    return RunChange("artifact_edit", run.stages[0].id, {"path": path, "text": text})


def events(storage: SqliteStorage, kind: str) -> list[dict[str, Any]]:
    with closing(connect(storage.path)) as conn:
        return [e.payload for e in read_after(conn, "world_1", 0) if e.type == kind]


def test_an_artifact_edit_applies_keeps_the_earlier_content_and_the_next_stage_reads_it(
    runner: Runner, storage: SqliteStorage, files: Files, tmp_path: Path
) -> None:
    run = at_gate(runner, [WRITE, TAG, CONFIRM, UPLOAD])
    change = artifact_edit(run, "metadata.md", FIVE)

    proposal = runner.propose(run.id, change)
    assert proposal.stage_name == "write" and proposal.violations == []
    assert (proposal.before["text"], proposal.after["text"]) == (METADATA, FIVE)
    assert proposal.read_by == ["tag"], "tag read the old version; upload has not started"
    made = stage_folder(tmp_path, run, 0) / "artifacts" / "metadata.md"
    assert made.read_text() == METADATA, "a proposal writes nothing"

    applied = runner.apply(run.id, change, proposal.revision, "operator")
    assert (applied.kind, applied.stage_name, applied.by) == ("artifact_edit", "write", "operator")
    assert made.read_text() == FIVE
    kept = tmp_path / "worlds" / "world_1" / applied.before["kept"]
    assert kept.read_text() == METADATA, "the earlier content is still there to view"
    assert fetch(runner, run.id).changes == [applied]
    created = events(storage, "artifact.created")
    assert created[-1]["path"] == str(made.relative_to(tmp_path / "worlds" / "world_1"))
    assert created[-1]["stage_id"] == run.stages[0].id
    assert events(storage, "run.changed")[-1]["id"] == applied.id

    with pytest.raises(Illegal, match="moved on since this was proposed: write was edited by"):
        runner.apply(run.id, change, proposal.revision, "operator")
    again = runner.propose(run.id, artifact_edit(run, "metadata.md", METADATA))
    runner.apply(run.id, artifact_edit(run, "metadata.md", METADATA), again.revision, "operator")
    second = fetch(runner, run.id).changes[-1]
    assert second.before["kept"] != applied.before["kept"], "every earlier version is kept"

    runner.apply(run.id, change, fetch(runner, run.id).revision, "operator")
    runner.decide(run.id)
    wait_for(lambda: fetch(runner, run.id).status == "done")
    assert files.read == {"tag": METADATA, "upload": FIVE}


def test_a_binary_file_is_refused(runner: Runner) -> None:
    run = at_gate(runner, [WRITE, CONFIRM, UPLOAD])
    change = artifact_edit(run, "short.mp4", "not a video")

    with pytest.raises(ValueError, match="short.mp4 is not a text file"):
        runner.propose(run.id, change)
    with pytest.raises(ValueError, match="short.mp4 is not a text file"):
        runner.apply(run.id, change, run.revision, "operator")
    with pytest.raises(ValueError, match="write made no file nothing.md"):
        runner.propose(run.id, artifact_edit(run, "nothing.md", ""))
    assert fetch(runner, run.id).changes == []


def test_an_artifact_that_no_longer_fits_its_type_is_refused_with_why(
    runner: Runner, tmp_path: Path
) -> None:
    def check(_run: Run, name: str, _old: str, text: str) -> list[str]:
        return [] if "#" in text else [f"{name} no longer fits type metadata: no tags"]

    runner.check_text = check
    run = at_gate(runner, [WRITE, CONFIRM, UPLOAD])
    untyped = artifact_edit(run, "metadata.md", "---\ntype: metadata\n---\n\nNo tags.\n")

    proposal = runner.propose(run.id, untyped)
    assert proposal.violations == ["metadata.md no longer fits type metadata: no tags"]
    with pytest.raises(ValueError, match="no longer fits type metadata: no tags"):
        runner.apply(run.id, untyped, proposal.revision, "operator")
    folder = stage_folder(tmp_path, run, 0) / "artifacts"
    assert (folder / "metadata.md").read_text() == METADATA
    assert not (folder.parent / "previous").exists()
    assert fetch(runner, run.id).changes == []


def test_a_file_a_running_stage_reads_is_refused(runner: Runner, files: Files) -> None:
    files.hold.clear()
    run = runner.start("world_1", "wf_1", 1, "Short", [WRITE, TAG, CONFIRM, UPLOAD])
    wait_for(lambda: fetch(runner, run.id).stages[1].status == "running")
    change = artifact_edit(fetch(runner, run.id), "metadata.md", FIVE)

    proposal = runner.propose(run.id, change)
    assert proposal.violations == [
        "tag is running and reads metadata.md: pause first or add a task"
    ]
    with pytest.raises(ValueError, match="tag is running and reads metadata.md"):
        runner.apply(run.id, change, proposal.revision, "operator")
    files.hold.set()
    wait_for(lambda: fetch(runner, run.id).status == "waiting_gate")
    assert files.read == {"tag": METADATA}


def test_a_file_a_later_stage_made_anew_is_refused(runner: Runner) -> None:
    remake = StageDef("remake", "swarm", {"goal": "Remake it.", "done_when": "Remade."})
    run = at_gate(runner, [WRITE, remake, CONFIRM, UPLOAD])

    proposal = runner.propose(run.id, artifact_edit(run, "metadata.md", FIVE))
    assert proposal.violations == ["remake made a newer metadata.md; edit that one"]


def test_a_change_that_cannot_be_recorded_leaves_the_file_as_it_was(
    runner: Runner, storage: SqliteStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = at_gate(runner, [WRITE, CONFIRM, UPLOAD])
    change = artifact_edit(run, "metadata.md", FIVE)

    def locked(*_: object) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage, "add_change", locked)
    with pytest.raises(RuntimeError, match="locked"):
        runner.apply(run.id, change, run.revision, "operator")
    folder = stage_folder(tmp_path, run, 0)
    assert (folder / "artifacts" / "metadata.md").read_text() == METADATA
    assert list((folder / "previous").iterdir()) == []
