"""tools/backup_data.py: what a slim backup leaves out, a live DB copied whole, the prune."""

import os
import sqlite3
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
    for i, age in enumerate((100, 200, 300)):
        old = dest / f"{backup_data.PREFIX}old{i}-20260101-0000.tgz"
        old.write_bytes(b"")
        os.utime(old, (age, age))
    monkeypatch.setattr(backup_data, "MIN_FREE", 0)

    backup_data.inside("t", root, dest, 2, False, f"{os.getuid()}:{os.getgid()}")
    live.close()

    old, new = sorted(dest.iterdir())  # "old2" sorts before "t"
    assert old.name == f"{backup_data.PREFIX}old2-20260101-0000.tgz"
    assert new.name.startswith(f"{backup_data.PREFIX}t-") and new.suffix == ".tgz"
    with tarfile.open(new) as tar:
        names = set(tar.getnames())
        tar.extract("swarmify.db", tmp_path / "x", filter="data")
    assert names == {*kept, "swarmify.db"}
    copy = sqlite3.connect(tmp_path / "x" / "swarmify.db")
    assert copy.execute("select x from t").fetchall() == [(1,)]
    copy.close()
    assert backup_data.to_prune(dest, 1) == [old]
