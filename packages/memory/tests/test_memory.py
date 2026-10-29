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


def test_a_stage_takes_another_runs_artifacts_across_worlds_latest_first(memory: Memory) -> None:
    memory.write_artifact("w2", "child", "s1", "worker-1", "brief.md", "old brief")
    memory.write_artifact("w2", "child", "s1", "worker-1", "ranks.csv", "a,1")
    memory.write_artifact("w2", "child", "s2", "worker-1", "brief.md", "new brief")
    # s0 is written last but comes first in the run: run order decides, not mtime
    memory.write_artifact("w2", "child", "s0", "worker-1", "brief.md", "stale brief")
    source = ("w2", "child", ["s0", "s1", "s2"])

    taken = memory.copy_artifacts(source, ["brief.md", "ranks.csv"], "w1", "r1", "s9", "c")
    assert taken == ["runs/r1/s9/artifacts/brief.md", "runs/r1/s9/artifacts/ranks.csv"]
    folder = memory.world_folder("w1") / "runs/r1/s9/artifacts"
    assert (folder / "brief.md").read_text().endswith("new brief")
    assert 'run: "child"' in (folder / "brief.md").read_text(), "provenance stays"
    assert (folder / "ranks.csv").read_text() == "a,1" and (folder / "ranks.csv.md").exists()
    assert [e["path"] for e in _events(memory)] == taken

    for bad in ("missing.md", "../brief.md", "*"):
        with pytest.raises(ValueError, match="no artifact named"):
            memory.copy_artifacts(source, ["brief.md", bad], "w1", "r2", "s9", "c")
    assert not (memory.world_folder("w1") / "runs/r2/s9/artifacts/brief.md").exists()


def test_an_artifact_may_carry_its_own_type_and_frontmatter(memory: Memory) -> None:
    fields = {"links": {"youtube": "https://youtu.be/v"}, "schedule": None}
    rel = memory.write_artifact(
        "w1", "r1", "s1", "publish", "publish.md", "# Hooks\n", "publish", fields
    )
    text = (memory.world_folder("w1") / rel).read_text()
    assert text.startswith('---\ntype: "publish"\nauthor: "publish"\n')
    assert 'links: {"youtube": "https://youtu.be/v"}\n' in text and "schedule" not in text
    assert _events(memory)[-1]["type"] == "publish"


def test_a_typed_artifact_carries_its_type_and_fields_in_frontmatter(memory: Memory) -> None:
    fields = {"title": "The chase", "rank": 1, "sources": ["runs/r1/s1/notes/a.md#p2"]}
    rel = memory.write_typed(
        "w1", "r1", "s1", "judge", "idea-chase.md", "idea", fields, "# Chase\n"
    )
    assert rel == "runs/r1/s1/artifacts/idea-chase.md"
    text = (memory.world_folder("w1") / rel).read_text()
    assert text.startswith('---\ntype: "idea"\nauthor: "judge"\n')
    assert 'rank: 1\nsources: ["runs/r1/s1/notes/a.md#p2"]\n---\n\n# Chase\n' in text
    assert _events(memory)[-1]["type"] == "idea"
    for bad in ("idea.txt", "../x.md"):
        with pytest.raises(ValueError):
            memory.write_typed("w1", "r1", "s1", "judge", bad, "idea", {}, "x")


def test_typed_fields_never_override_the_reserved_frontmatter_keys(memory: Memory) -> None:
    spoof = {"type": "verdict", "author": "operator", "run": "other", "stage": "x", "hook": "h"}
    memory.write_typed("w1", "r1", "s1", "judge", "idea-evil.md", "idea", spoof, "Body")
    (art,) = memory.stage_artifacts("w1", "r1", "s1")
    assert (art.type, art.author) == ("idea", "judge")
    assert (art.fields["run"], art.fields["stage"], art.fields["hook"]) == ("r1", "s1", "h")


def test_notes_and_stage_artifacts_read_back_without_frontmatter(memory: Memory) -> None:
    memory.write_note("w1", "r1", "s1", "worker-1", "Trends", "Hooks win.\n\nChases.")
    memory.write_typed(
        "w1", "r1", "s1", "judge", "idea-a.md", "idea", {"title": "A", "rank": 1}, "Body"
    )
    memory.write_artifact("w1", "r1", "s1", "worker-1", "ranks.csv", "a,1")
    memory.write_artifact("w1", "r1", "s1", "worker-1", "brief.md", "Brief")
    folder = memory.world_folder("w1") / "runs/r1/s1/artifacts"
    (folder / "hand.md").write_text("---\nodd: not json\n---\n\nHand")
    assert memory.notes("w1", "r1", "s1") == [
        ("runs/r1/s1/notes/trends.md", "# Trends\n\nHooks win.\n\nChases.\n")
    ]
    assert memory.notes("w1", "r1", "s_none") == []
    arts = {a.name: a for a in memory.stage_artifacts("w1", "r1", "s1")}
    assert sorted(arts) == ["brief.md", "hand.md", "idea-a.md", "ranks.csv"]
    idea = arts["idea-a.md"]
    assert (idea.type, idea.author, idea.title, idea.body) == ("idea", "judge", "A", "Body")
    assert idea.fields["rank"] == 1 and idea.path == "runs/r1/s1/artifacts/idea-a.md"
    assert (arts["ranks.csv"].type, arts["ranks.csv"].path) == (
        "file",
        "runs/r1/s1/artifacts/ranks.csv.md",
    )
    assert arts["brief.md"].body == "Brief" and arts["brief.md"].title == "brief.md"
    assert arts["hand.md"].fields == {"odd": "not json"} and arts["hand.md"].type == "file"


def _decide(memory: Memory, run: str, created: str) -> None:
    folder = memory.world_folder("w1") / "runs" / run / "02-pick"
    folder.mkdir(parents=True)
    front = f'type: "decision"\nrun: "{run}"\nstage: "pick"\ncreated: "{created}"'
    (folder / "decision.md").write_text(f"---\n{front}\n---\n\nPicked 1.\n")


def test_the_library_lists_every_artifact_of_the_world_across_runs_newest_first(
    memory: Memory,
) -> None:
    memory.write_typed("w1", "r1", "s1", "judge", "idea-a.md", "idea", {"title": "A"}, "Body")
    memory.write_artifact("w1", "r1", "s3", "worker-1", "clip.mp4", "bytes")
    memory.write_note("w1", "r1", "s1", "worker-1", "Not an artifact", "x")
    memory.write_artifact("w2", "r9", "s1", "worker-1", "other.md", "other world")
    _decide(memory, "r1", "2026-10-26T10:00:00+00:00")
    memory.write_artifact("w1", "r2", "s4", "worker-1", "script.md", "Script")

    items = memory.library("w1")
    assert items[0].name == "script.md", "newest first"
    assert items[-1].type == "decision", "a hand-dated decision from before the others"
    assert {(a.run_id, a.stage, a.name, a.type) for a in items} == {
        ("r1", "s1", "idea-a.md", "idea"),
        ("r1", "s3", "clip.mp4", "file"),
        ("r1", "02-pick", "decision.md", "decision"),
        ("r2", "s4", "script.md", "file"),
    }
    clip = next(a for a in items if a.name == "clip.mp4")
    assert clip.path == "runs/r1/s3/artifacts/clip.mp4.md" and clip.created
    assert not any(a.pinned for a in items) and memory.pinned("w1") == []
    assert memory.library("w_none") == []


def test_a_pin_lives_in_the_frontmatter_and_the_pinned_list_follows_it(memory: Memory) -> None:
    brief = memory.write_artifact("w1", "r1", "s1", "worker-1", "brief.md", "Brief")
    csv = memory.write_artifact("w1", "r1", "s1", "worker-1", "ranks.csv", "a,1")
    pinned = memory.pin("w1", brief)
    assert (pinned.pinned, pinned.path, pinned.run_id) == (True, brief, "r1")
    text = (memory.world_folder("w1") / brief).read_text()
    assert "\npinned: true\n---\n\nBrief" in text and text.startswith('---\ntype: "file"')
    sidecar = memory.pin("w1", csv)  # a binary: its sidecar carries the flag
    assert sidecar.path == f"{csv}.md" and sidecar.name == sidecar.title == "ranks.csv"
    assert "pinned: true" in (memory.world_folder("w1") / f"{csv}.md").read_text()
    assert sorted(memory.pinned("w1")) == [brief, f"{csv}.md"]
    assert all(a.pinned for a in memory.stage_artifacts("w1", "r1", "s1"))

    unpinned = memory.pin("w1", brief, pinned=False)
    assert not unpinned.pinned and "pinned" not in (memory.world_folder("w1") / brief).read_text()
    assert memory.pinned("w1") == [f"{csv}.md"]
    assert [e for e in _events(memory) if "pinned" in e] == [
        {"type": "file", "path": brief, "pinned": True},
        {"type": "file", "path": f"{csv}.md", "pinned": True},
        {"type": "file", "path": brief, "pinned": False},
    ]
    for bad in ("runs/r1/s1/artifacts/missing.md", "../escape.md", "/etc/hosts", "runs/r1/s1"):
        with pytest.raises(ValueError, match="no artifact at"):
            memory.pin("w1", bad)


@pytest.mark.parametrize("root", ["symlink", "relative"])
def test_a_pin_works_when_the_data_root_is_a_symlink_or_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    db = real / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, MIGRATIONS)
    if root == "symlink":
        (tmp_path / "link").symlink_to(real)
        memory = Memory(db, tmp_path / "link")
    else:
        monkeypatch.chdir(tmp_path)
        memory = Memory(db, Path("real"))
    brief = memory.write_artifact("w1", "r1", "s1", "worker-1", "brief.md", "Brief")
    assert [a.path for a in memory.library("w1")] == [brief]
    assert memory.pin("w1", brief).pinned and memory.pinned("w1") == [brief]


def test_assets_are_the_operators_files_listed_served_and_never_a_path(memory: Memory) -> None:
    assert memory.assets("w1") == []
    assert memory.add_asset("w1", "bed-calm.mp3", b"ID3") == "assets/bed-calm.mp3"
    assets = memory.world_folder("w1") / "assets"
    (assets / "sfx").mkdir()
    (assets / "sfx/whoosh.wav").write_bytes(b"RIFF")  # dropped in by hand
    (assets / ".DS_Store").write_bytes(b"")
    assert memory.assets("w1") == ["assets/bed-calm.mp3", "assets/sfx/whoosh.wav"]
    assert memory.file("w1", "assets/sfx/whoosh.wav").read_bytes() == b"RIFF"
    for bad in ("../x.mp3", "sfx/x.mp3", ".env", ""):
        with pytest.raises(ValueError):
            memory.add_asset("w1", bad, b"x")
    assert memory.library("w1") == []  # not run artifacts
