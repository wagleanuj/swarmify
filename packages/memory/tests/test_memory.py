"""Notes and artifacts land as markdown files with frontmatter and an `artifact.created` event."""

from contextlib import closing
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import Memory


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    return Memory(db, tmp_path)


def _events(memory: Memory) -> list[dict[str, object]]:
    with closing(connect(memory.db)) as conn:
        return [e.payload for e in read_after(conn, "w1", 0)]


def test_a_note_is_markdown_with_frontmatter_in_the_stage_folder(memory: Memory) -> None:
    rel = memory.write_note("w1", "r1", "s1", "worker-1", "Hooks: what wins?", "Short ones.", "t1")
    assert rel == "runs/r1/s1/notes/hooks-what-wins.md"
    text = (memory.world_folder("w1") / rel).read_text()
    assert text.startswith('---\ntype: "note"\nauthor: "worker-1"\nrun: "r1"\nstage: "s1"\n')
    assert 'task: "t1"' in text and text.endswith("---\n\n# Hooks: what wins?\n\nShort ones.\n")
    again = memory.write_note("w1", "r1", "s1", "worker-1", "Hooks: what wins?", "More.")
    assert again == "runs/r1/s1/notes/hooks-what-wins-2.md"
    assert "task:" not in (memory.world_folder("w1") / again).read_text()
    assert _events(memory)[0] == {
        "type": "note",
        "path": rel,
        "author": "worker-1",
        "stage_id": "s1",
    }
    with pytest.raises(ValueError):
        memory.write_note("w1", "r1", "s1", "worker-1", "  ", "x")


def test_artifacts_are_plain_names_and_non_markdown_gets_a_sidecar(memory: Memory) -> None:
    md = memory.write_artifact("w1", "r1", "s1", "worker-1", "ideas.md", "1. Hooks")
    csv = memory.write_artifact("w1", "r1", "s1", "worker-1", "ranks.csv", "a,1")
    folder = memory.world_folder("w1") / "runs/r1/s1/artifacts"
    assert (md, csv) == ("runs/r1/s1/artifacts/ideas.md", "runs/r1/s1/artifacts/ranks.csv")
    assert (folder / "ideas.md").read_text().endswith("---\n\n1. Hooks")
    assert (folder / "ranks.csv").read_text() == "a,1"
    assert (folder / "ranks.csv.md").read_text().startswith('---\ntype: "file"')
    assert memory.artifacts("w1", "r1") == [
        "s1/artifacts/ideas.md",
        "s1/artifacts/ranks.csv",
    ]
    assert [e["type"] for e in _events(memory)] == ["file", "file"]
    for bad in ("../escape.md", "a/b.md", ".hidden", ""):
        with pytest.raises(ValueError):
            memory.write_artifact("w1", "r1", "s1", "worker-1", bad, "x")
