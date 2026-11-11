"""What a world already made, read from its files alone: the picked or only idea of every run that
went on to make something, and every publish record, so research never pitches them again
whether or not the graph is current."""

import re
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
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return memory.write_typed(
        "w1", run, "s1", "judge", f"idea-{slug}.md", "idea",
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
    retention = {"youtube_avg_viewed_percent": 71.25, "youtube_avg_view_seconds": 15.5}
    memory.record_outcome("w1", publish, {"views": 28000, **retention}, "views", NOW)
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
    # retention, so research and scoring see what held viewers, not only how many came
    assert "youtube avg viewed 71.2%" in published and "youtube avg view 15.5 s" in published
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


def _decision(memory: Memory, run: str, stage: str, kind: str, at: str, **fields: str) -> None:
    folder = memory.stage_folder("w1", run, stage)
    extra = "".join(f'{k}: "{v}"\n' for k, v in fields.items())
    (folder / "decision.md").write_text(
        f'---\ntype: "decision"\nrun: "{run}"\nkind: "{kind}"\n{extra}created: "{at}"\n---\n\n'
        "# Decision\n\nDone.\n"
    )


def _video(memory: Memory, run: str) -> str:
    """A rendered video with its sidecar, as the box's review-video gate approves it."""
    folder = memory.stage_folder("w1", run, "render") / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "video.mp4").write_bytes(b"\0" * 16)
    (folder / "video.mp4.md").write_text(
        f'---\ntype: "file"\nrun: "{run}"\ncreated: "2026-11-07T12:00:00+00:00"\n---\n\nThe cut.\n'
    )
    return str((folder / "video.mp4.md").relative_to(memory.world_folder("w1")))


def _posted(memory: Memory, run: str, stage: str, title: str) -> None:
    memory.write_artifact(
        "w1", run, stage, "publish", "publish.md", "# Posted\n\nWent out.\n",
        kind="publish", fields={"title": title},
    )  # fmt: skip


def test_the_pick_gate_names_the_run_never_the_reviewed_video(memory: Memory) -> None:
    # Like bcs-05: a pick by choice alone (the card has that title), then a review gate that
    # approved video.mp4.md, and two publish records, one titled only "Posted to meta".
    for run, title in [("r1", "Your Cat Won't Work For Food"), ("r2", "Stink Face")]:
        _idea(memory, run, title, study="Delgado 2021")
        _idea(memory, run, f"{title} runner-up")
        _decision(memory, run, "pick", "pick", "2026-11-07T07:00:00+00:00", choice=title)
        _decision(
            memory, run, "review", "approve", "2026-11-07T12:56:00+00:00",
            choice="video.mp4", artifact=_video(memory, run),
        )  # fmt: skip
        _posted(memory, run, "yt", f"{title}. Rats Will.")
        _posted(memory, run, "meta", "Posted to meta")

    lines = made(memory.world_folder("w1"), {})

    assert len(lines) == 2
    assert not any("video.mp4" in line for line in lines)
    work = next(line for line in lines if line.startswith("- Your Cat Won't Work For Food ("))
    assert "Delgado 2021" in work and "posted as Your Cat Won't Work For Food. Rats Will." in work
    assert any(line.startswith("- Stink Face (") for line in lines)


def test_a_pick_of_a_whole_list_is_named_by_its_choice(memory: Memory) -> None:
    # Like run_01M70641: the pick points at ideas.md with a choice; the run's only idea card
    # is another idea, so it must not stand in for the choice.
    for run in ("r1", "r2"):  # two runs that picked the same: both stay
        ideas = memory.write_artifact(
            "w1", run, "research", "judge", "ideas.md", "# Five ideas\n\n## 4. GPS Pin\n"
        )
        _idea(memory, run, "Your Cat's 5 AM Purr")
        _decision(
            memory, run, "pick", "pick", "2026-11-09T13:30:00+00:00",
            choice="#4 Your Cat Has A GPS Pin On You", artifact=ideas,
        )  # fmt: skip
        _posted(memory, run, "yt", "Your Cat Seems To Keep A Map Of You")

    lines = made(memory.world_folder("w1"), {})

    assert len(lines) == 2
    for line in lines:
        assert line.startswith("- Your Cat Has A GPS Pin On You (published")
        assert "posted as Your Cat Seems To Keep A Map Of You" in line
        assert "5 AM Purr" not in line


def test_with_no_pick_a_run_falls_back_to_its_metadata_title(memory: Memory) -> None:
    _posted(memory, "r1", "meta", "Posted to meta")
    memory.write_artifact(
        "w1",
        "r1",
        "write",
        "writer",
        "metadata.md",
        "# Metadata\n\nThe gist.\n",
        fields={"title": "Why Your Cat Stares At The Wall"},
    )
    (line,) = made(memory.world_folder("w1"), {})
    assert line.startswith("- Why Your Cat Stares At The Wall (published")
