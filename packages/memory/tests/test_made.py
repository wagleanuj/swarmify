"""What a world already made, read from its files alone: the picked or only idea of every run that
went on to make something, and every publish record, so research never pitches them again
whether or not the graph is current."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swarmify_kernel import MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate
from swarmify_memory import Memory, made

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    db = tmp_path / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    return Memory(db, tmp_path)


def _idea(memory: Memory, run: str, title: str, **fields: str) -> str:
    return memory.write_typed(
        "w1", run, "s1", "judge", f"idea-{title.lower().replace(' ', '-')}.md", "idea",
        {"title": title, "rank": 1, **fields}, f"# {title}\n\nThe pitch of {title}.\n",
    )  # fmt: skip


def _pick(memory: Memory, run: str, artifact: str) -> None:
    folder = memory.stage_folder("w1", run, "s2")
    (folder / "decision.md").write_text(
        f'---\ntype: "decision"\nrun: "{run}"\nkind: "pick"\nartifact: "{artifact}"\n'
        f'created: "2026-11-0{run[-1]}T00:00:00+00:00"\n---\n\n# Decision\n\nPicked it.\n'
    )


def test_published_and_finished_runs_are_listed_with_their_core_fact_and_outcome(
    memory: Memory,
) -> None:
    # r1: research ranked two ideas, the second was picked, made and published.
    _idea(memory, "r1", "Cats sleep all day")
    chosen = _idea(memory, "r1", "Contrafreeloading", study="Delgado 2021", hook="Free food?")
    _pick(memory, "r1", chosen)
    publish = memory.write_artifact(
        "w1", "r1", "s9", "publish", "publish.md", "# Posted\n\nWent to youtube.\n",
        kind="publish", fields={"title": "Why cats work for food"},
    )  # fmt: skip
    memory.record_outcome("w1", publish, {"views": 28000}, "views", NOW)
    # r2: one idea, still being made. r3: research only, nothing picked. r4: aborted.
    _idea(memory, "r2", "Slow blink", fact="A slow blink means trust")
    _idea(memory, "r3", "Idea A")
    _idea(memory, "r3", "Idea B")
    _idea(memory, "r4", "Abandoned")

    runs = {"r1": "done", "r2": "running", "r3": "done"}  # what the caller counts: r4 is not
    lines = made(memory.world_folder("w1"), runs)

    assert lines == memory.made("w1", runs)
    assert len(lines) == 2
    published = next(line for line in lines if "Contrafreeloading" in line)
    assert "published" in published and "28,000 views" in published
    assert "posted as Why cats work for food" in published
    assert "Delgado 2021" in published and "Free food?" in published
    assert "The pitch of Contrafreeloading." in published
    assert "Cats sleep all day" not in "\n".join(lines)  # ranked but never picked
    making = next(line for line in lines if "Slow blink" in line)
    assert "in the making" in making and "A slow blink means trust" in making
    assert made(memory.world_folder("w1"), {}) == [published]  # a publish record always counts
    assert made(memory.world_folder("nowhere"), runs) == []


def test_a_published_run_with_no_idea_is_listed_by_its_publish_title(memory: Memory) -> None:
    memory.write_artifact(
        "w1", "r1", "s9", "publish", "publish.md", "# Posted\n\nA video about bowls.\n",
        kind="publish", fields={"title": "Bowl cut science"},
    )  # fmt: skip
    (line,) = made(memory.world_folder("w1"), {})
    assert line.startswith("- Bowl cut science (published") and "A video about bowls." in line
