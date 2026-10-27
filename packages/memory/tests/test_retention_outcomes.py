"""Retention deletes old run binaries and keeps every markdown record; outcomes land in the
publish artifact's frontmatter and body."""

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


def test_an_outcome_lands_in_frontmatter_and_body_replacing_the_last_one(memory: Memory) -> None:
    body = "# Five hooks\n\nshort.mp4 went to youtube: public, now.\n\n- youtube: https://youtu.be/v1\n"
    path = memory.write_artifact(
        "w1", "r1", "s9", "publish", "publish.md", body, kind="publish", fields={"id": "v1"}
    )
    [(found, fields)] = memory.publish_records("w1")
    assert (found, fields["type"], fields["id"]) == (path, "publish", "v1")

    memory.record_outcome("w1", path, 41000, {"v1": 41000}, NOW)
    memory.record_outcome("w1", path, 41250, {"v1": 41250}, NOW + timedelta(days=1))

    text = (memory.world_folder("w1") / path).read_text()
    [(_, fields)] = memory.publish_records("w1")
    assert fields["views"] == 41250 and fields["views_at"] == "2026-10-27T12:00:00+00:00"
    assert text.count("## Outcome") == 1
    assert text.endswith(
        "- youtube: https://youtu.be/v1\n\n## Outcome\n\n41,250 views as of 2026-10-27.\n\n"
        "- v1: 41,250 views\n"
    )
    recorded = [(t, p) for t, p in _events(memory) if t == "outcome.recorded"]
    assert recorded[-1] == ("outcome.recorded", {"path": path, "views": 41250})
    with pytest.raises(ValueError, match="not a file"):
        memory.record_outcome("w1", "../../escape.md", 1, {}, NOW)
