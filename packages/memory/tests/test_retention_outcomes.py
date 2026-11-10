"""Retention deletes old run binaries and keeps every markdown record; outcomes land in a
sidecar beside the publish artifact, which they never rewrite."""

import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import Memory

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    return Memory(db, tmp_path)


def _events(memory: Memory) -> list[tuple[str, dict[str, object]]]:
    with closing(connect(memory.db)) as conn:
        return [(e.type, e.payload) for e in read_after(conn, "w1", 0)]


def _age(path: Path, days: int) -> None:
    then = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (then, then))


def test_prune_deletes_old_binaries_and_keeps_markdown_and_sidecars(memory: Memory) -> None:
    memory.write_artifact("w1", "r1", "s1", "render", "short.mp4", "video bytes")
    memory.write_artifact("w1", "r1", "s1", "worker-1", "ideas.md", "1. Hooks")
    memory.write_note("w1", "r1", "s1", "worker-1", "hooks", "Short ones win.")
    memory.write_artifact("w1", "r2", "s1", "render", "fresh.mp4", "new bytes")
    run = memory.world_folder("w1") / "runs"
    loose = run / "r1/s1/frame.png"  # a binary no sidecar describes
    loose.write_bytes(b"\x89PNG")
    for path in run.rglob("*"):
        if path.is_file() and "r2" not in path.parts:
            _age(path, 100)

    gone = memory.prune("w1", NOW - timedelta(days=90), NOW)

    assert gone == ["runs/r1/s1/artifacts/short.mp4", "runs/r1/s1/frame.png"]
    assert not (run / "r1/s1/artifacts/short.mp4").exists() and not loose.exists()
    assert (run / "r2/s1/artifacts/fresh.mp4").exists()
    left = sorted(str(p.relative_to(run)) for p in run.rglob("*") if p.is_file())
    assert left == [
        "r1/s1/artifacts/ideas.md",
        "r1/s1/artifacts/short.mp4.md",
        "r1/s1/frame.png.md",
        "r1/s1/notes/hooks.md",
        "r2/s1/artifacts/fresh.mp4",
        "r2/s1/artifacts/fresh.mp4.md",
    ]
    sidecar = (run / "r1/s1/artifacts/short.mp4.md").read_text()
    assert 'author: "render"' in sidecar and f'deleted: "{NOW.isoformat()}"' in sidecar
    assert "short.mp4 was deleted by retention" in sidecar and "This record stays." in sidecar
    [card] = [a for a in memory.stage_artifacts("w1", "r1", "s1") if a.name == "short.mp4"]
    assert card.fields["deleted"] and card.type == "file"
    assert "frame.png was deleted" in (run / "r1/s1/frame.png.md").read_text()
    pruned = [p for t, p in _events(memory) if t == "artifact.pruned"]
    assert pruned == [{"path": "runs/r1/s1/artifacts/short.mp4"}, {"path": "runs/r1/s1/frame.png"}]

    assert memory.prune("w1", NOW - timedelta(days=90), NOW) == []  # nothing left to delete
    assert memory.prune("w2", NOW, NOW) == []  # a world without runs


def test_an_outcome_lives_beside_the_publish_record_which_never_changes_for_it(
    memory: Memory,
) -> None:
    """View counts move every few hours; the publish record is what the graph extracts, so they
    go in a sidecar that the graph skips, and every reader sees them merged into the fields."""
    body = "# Five hooks\n\nshort.mp4 went to youtube: public, now.\n\n- youtube: https://youtu.be/v1\n"
    path = memory.write_artifact(
        "w1", "r1", "s9", "publish", "publish.md", body, kind="publish", fields={"id": "v1"}
    )
    record = memory.world_folder("w1") / path
    written = record.read_text()

    memory.record_outcome("w1", path, {"views": 41000}, "views", NOW)
    memory.record_outcome(
        "w1", path, {"views": 41250, "clicks": 3}, "views", NOW + timedelta(days=1)
    )

    assert record.read_text() == written
    sidecar = record.with_name(".publish.md.outcomes.json")
    assert sidecar.is_file()
    [(found, fields)] = memory.publish_records("w1")
    assert (found, fields["type"], fields["id"]) == (path, "publish", "v1")
    assert fields["outcomes"] == {"views": 41250, "clicks": 3}
    assert (fields["outcomes_at"], fields["primary_metric"]) == (
        "2026-10-27T12:00:00+00:00",
        "views",
    )
    [card] = memory.stage_artifacts("w1", "r1", "s9")
    assert card.name == "publish.md" and card.fields["outcomes"] == {"views": 41250, "clicks": 3}
    assert [a.name for a in memory.library("w1")] == ["publish.md"]  # the sidecar is no artifact
    recorded = [(t, p) for t, p in _events(memory) if t == "outcome.recorded"]
    assert recorded[-1] == (
        "outcome.recorded",
        {"path": path, "metrics": {"views": 41250, "clicks": 3}, "primary": "views"},
    )
    with pytest.raises(ValueError, match="not a file"):
        memory.record_outcome("w1", "../../escape.md", {}, "views", NOW)

    # Retention keeps it however old: it is the record's, not a run binary.
    _age(sidecar, 400)
    assert memory.prune("w1", NOW - timedelta(days=90), NOW) == [] and sidecar.is_file()


def test_a_record_with_outcomes_in_its_text_is_read_and_moved_out_on_the_next_outcome(
    memory: Memory,
) -> None:
    """Records from before the sidecar keep their outcome in the frontmatter (and `views` before
    outcomes were generic) and an Outcome section: read as they are, cleaned once on the next
    outcome, then left alone."""
    old = {"id": "v1", "outcomes": {"views": 9}, "outcomes_at": "2026-10-01", "views": 9}
    body = "# Five hooks\n\n- youtube: https://youtu.be/v1\n\n## Outcome\n\n9 views, 2026-10-01.\n"
    path = memory.write_artifact(
        "w1", "r1", "s9", "publish", "publish.md", body, kind="publish", fields=old
    )
    [(_, fields)] = memory.publish_records("w1")
    assert fields["outcomes"] == {"views": 9}

    memory.record_outcome("w1", path, {"views": 12}, "views", NOW)
    text = (memory.world_folder("w1") / path).read_text()
    assert "## Outcome" not in text and "outcomes" not in text and "views" not in text
    assert text.endswith("- youtube: https://youtu.be/v1\n")
    [(_, fields)] = memory.publish_records("w1")
    assert fields["outcomes"] == {"views": 12} and "views" not in fields
    memory.record_outcome("w1", path, {"views": 15}, "views", NOW)
    assert (memory.world_folder("w1") / path).read_text() == text
