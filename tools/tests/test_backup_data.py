"""tools/backup_data.py: what a slim backup leaves out, a live DB copied whole, the prune and
the space check."""

import os
import sqlite3
import subprocess
import tarfile

from tools import backup_data


def test_backup_leaves_out_what_can_be_made_again_and_keeps_the_newest(tmp_path, monkeypatch):
    root, dest = tmp_path / "data", tmp_path / "out"
    dest.mkdir()
    kept = [
        "setup.json",
        "claude/projects/a.jsonl",
        "worlds/w/graphify-out/graph.json",
        "worlds/w/runs/r/s/artifacts/video.mp4",
        "worlds/w/runs/r/s/publish.json",
        "worlds/w/runs/r/s/publish.outcomes.json",
        "worlds/w/assets/motionlib/clips.json",
        "worlds/w/assets/build/keep.txt",
    ]
    dropped = [
        "packages/p/node_modules/x.js",
        "npm-cache/x",
        "worlds/w/runs/r/s/build/out.mp4",
        "worlds/w/runs/r/s/frames/0001.png",
        "worlds/w/assets/motionlib/.store/clip.bin",
    ]
    for rel in kept + dropped:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    live = sqlite3.connect(root / "swarmify.db")
    live.execute("pragma journal_mode=wal")
    live.execute("create table t (x)")
    live.execute("insert into t values (1)")
    live.commit()  # left in the WAL, the connection still open
    # older backups in both formats count toward keep; empty archives that read back whole
    for i, (age, suffix) in enumerate(((100, ".tgz"), (200, ".tar.zst"), (300, ".tgz"))):
        old = dest / f"{backup_data.PREFIX}old{i}-20260101-0000{suffix}"
        with tarfile.open(old.with_suffix(".tar"), "w"):
            pass
        pack = ["zstd", "-q"] if suffix == ".tar.zst" else ["gzip"]
        with old.with_suffix(".tar").open("rb") as f, old.open("wb") as out:
            subprocess.run(pack, stdin=f, stdout=out, check=True)
        old.with_suffix(".tar").unlink()
        os.utime(old, (age, age))
    (dest / f"{backup_data.PREFIX}x-20260101-0000.tar.zst.part").write_bytes(b"")  # not counted
    monkeypatch.setattr(backup_data, "MIN_FREE", 0)

    backup_data.inside("t", root, dest, 2, False, f"{os.getuid()}:{os.getgid()}")
    live.close()

    old, new = backup_data.backups(dest)[::-1]
    assert old.name == f"{backup_data.PREFIX}old2-20260101-0000.tgz"
    assert new.name.startswith(f"{backup_data.PREFIX}t-") and new.name.endswith(".tar.zst")
    tar_bytes = subprocess.run(["zstd", "-dc", new], capture_output=True, check=True).stdout
    (tmp_path / "new.tar").write_bytes(tar_bytes)
    with tarfile.open(tmp_path / "new.tar") as tar:
        names = set(tar.getnames())
        tar.extract("swarmify.db", tmp_path / "x", filter="data")
    assert names == {*kept, "swarmify.db"}
    copy = sqlite3.connect(tmp_path / "x" / "swarmify.db")
    assert copy.execute("select x from t").fetchall() == [(1,)]
    copy.close()
    assert backup_data.to_prune(dest, 1) == [old]


def test_dry_run_counts_what_keep_prunes_anyway_and_prints_the_plan(tmp_path, monkeypatch, capsys):
    """The box's case: 9.4 GB of data, 18 GB free, two old backups of 17.5 GB that keep 3
    drops anyway behind two newer ones."""
    gb = 2**30
    dest = tmp_path / "out"
    dest.mkdir()
    for i, (size, age) in enumerate(((17.5, 100), (17.5, 200), (5.9, 300), (5.9, 400))):
        old = dest / f"{backup_data.PREFIX}b{i}-20260101-0000{('.tgz', '.tar.zst')[i % 2]}"
        with old.open("wb") as f:
            f.truncate(int(size * gb))  # sparse, only its size counts
        os.utime(old, (age, age))
    data = [("frames.mp4", 4.6 * gb), ("db.jsonl", 4.8 * gb)]  # 1:1 and 3:1
    est = sum(int(n) if r.endswith(".mp4") else int(n) // 3 for r, n in data)
    files = [r for r, _ in data]
    monkeypatch.setattr(backup_data, "plan", lambda root: (files, [], int(9.4 * gb), est))

    class Disk:
        f_bavail, f_frsize = 18 * gb // 4096, 4096

    monkeypatch.setattr(backup_data.os, "statvfs", lambda _: Disk)

    backup_data.inside("box", tmp_path / "data", dest, 3, True, "0:0")
    out = capsys.readouterr().out
    assert "archive about 6.2 GB" in out
    assert "pruned first: 35.0 GB, after: about 46.8 GB" in out
    assert out.count("would keep") == 2 and out.count("would delete") == 2

    def named(verb: str) -> set[str]:
        return {ln.split("-backup-")[1][:2] for ln in out.splitlines() if ln.startswith(verb)}

    assert named("would keep") == {"b2", "b3"} and named("would delete") == {"b0", "b1"}
    assert "would write" in out

    monkeypatch.setattr(backup_data, "MIN_FREE", 50 * gb)  # still prints the plan, then refuses
    try:
        backup_data.inside("box", tmp_path / "data", dest, 3, True, "0:0")
    except SystemExit as e:
        assert "refusing" in str(e)
    else:
        raise AssertionError("did not refuse")
    out = capsys.readouterr().out
    assert out.count("would delete") == 2 and "would write" in out
    assert len(list(dest.iterdir())) == 4  # a dry run deletes nothing

    # keep 1 still keeps one old backup until the new one is written
    early, stay = backup_data.early_prune(dest, 1)
    assert len(stay) == 1 and len(early) == 3


def test_verify_refuses_a_truncated_archive(tmp_path):
    good = tmp_path / f"{backup_data.PREFIX}a-20260101-0000.tar.zst"
    src = tmp_path / "f"
    src.write_bytes(os.urandom(1 << 16))
    with tarfile.open(tmp_path / "a.tar", "w") as tar:
        tar.add(src, arcname="f")
    subprocess.run(["zstd", "-q", tmp_path / "a.tar", "-o", good], check=True)
    backup_data.verify(good)
    bad = tmp_path / "bad.tar.zst.part"
    bad.write_bytes(good.read_bytes()[:-100])
    try:
        backup_data.verify(bad)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a truncated archive passed")
