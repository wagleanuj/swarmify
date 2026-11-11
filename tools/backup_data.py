"""Back up the `data` volume slim (README, Deployment, Backups): run only when a deploy changes
stored data, keep the newest 3.

    python3 tools/backup_data.py LABEL [--dest /data] [--keep 3] [--dry-run]
                                 [--volume swarmify_data] [--image swarmify-api]

Run on the host as a user in the docker group, no sudo: it starts itself again in a throwaway
container of the api image with the volume at /data and DEST at /out. There every SQLite file is
copied with SQLite's backup API, never tarred live, and the rest goes in as is, less what can be
made again: node_modules, caches, a run's build and frames folders and the motionlib store. The
archive is read back in full before the oldest beyond --keep are deleted. The backups that keep-N
would delete anyway (all but the newest keep-1) go first, but only once the keep-1 that stay read
back whole. It refuses to start if the archive, estimated at 1:1 for media and archives and 3:1 for
the rest, would leave under 10 GB free in DEST once those are gone. --dry-run prints the whole
plan, the refusal included.

It runs on the host's own Python (3.8 on Ubuntu 20.04), stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

PREFIX = "swarmify-data-backup-"
MIN_FREE = 10 * 2**30
# Paths under the volume's root that can be made again; node_modules anywhere.
EXCLUDE = (
    "npm-cache",
    "sandbox/cache",
    "worlds/*/runs/*/*/build",
    "worlds/*/runs/*/*/frames",
    "worlds/*/assets/motionlib/.store",
)
SQLITE_EXT = (".db", ".sqlite", ".sqlite3")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Already compressed, so counted at full size; the rest is counted at a third.
# ponytail: a fixed 3:1 guess, sample real ratios if it misjudges a volume.
COMPRESSED_EXT = (".png", ".jpg", ".jpeg", ".mp4", ".wav", ".webm", ".zip", ".tgz", ".gz")


def excluded(rel: str) -> bool:
    parts = rel.split("/")
    if "node_modules" in parts:
        return True
    return any(len(parts) == p.count("/") + 1 and fnmatch.fnmatchcase(rel, p) for p in EXCLUDE)


def is_sqlite(path: Path) -> bool:
    if not path.name.endswith(SQLITE_EXT):
        return False
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def plan(root: Path) -> tuple[list[str], list[str], int, int]:
    """The files to tar and the SQLite files to copy first (paths relative to root), their
    total size and the archive's estimated size."""
    files: list[str] = []
    dbs: list[str] = []
    size = est = 0
    for top, dirs, names in os.walk(root):
        rel_top = os.path.relpath(top, root)
        rel_top = "" if rel_top == "." else rel_top + "/"
        dirs[:] = [d for d in dirs if not excluded(rel_top + d)]
        for name in names:
            rel, path = rel_top + name, Path(top) / name
            if excluded(rel) or path.is_symlink() or not path.is_file():
                continue
            if name.endswith(SQLITE_SIDECARS) and name.rsplit("-", 1)[0].endswith(SQLITE_EXT):
                continue  # the copy below folds in the WAL
            (dbs if is_sqlite(path) else files).append(rel)
            n = path.stat().st_size
            size += n
            est += n if name.lower().endswith(COMPRESSED_EXT) else n // 3
    return files, dbs, size, est


def backups(dest: Path) -> list[Path]:
    """The backups in dest, newest first."""
    return sorted(dest.glob(PREFIX + "*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)


def to_prune(dest: Path, keep: int) -> list[Path]:
    """The backups in dest beyond the newest `keep`."""
    return backups(dest)[max(keep, 0) :]


def verify(archive: Path) -> None:
    with gzip.open(archive, "rb") as f:
        while f.read(1 << 20):  # the gzip CRC is checked at the end
            pass
    with tarfile.open(archive, "r:gz") as tar:
        tar.getmembers()


def early_prune(dest: Path, keep: int) -> tuple[list[Path], list[Path]]:
    """The backups keep-N deletes anyway once the new one counts as one of `keep` (all but
    the newest keep-1), and the ones that stay. None go early unless at least one stays."""
    found = backups(dest)
    stay = max(keep - 1, 1)  # never write the new one with no backup left
    return found[stay:], found[:stay]


def inside(label: str, root: Path, dest: Path, keep: int, dry: bool, owner: str) -> None:
    files, dbs, size, est = plan(root)
    free = os.statvfs(dest).f_bavail * os.statvfs(dest).f_frsize
    name = f"{PREFIX}{label}-{time.strftime('%Y%m%d-%H%M')}.tgz"
    gb = 2**30
    early, stay = early_prune(dest, keep)
    freed = sum(p.stat().st_size for p in early)
    after = free + freed - est
    print(f"{len(files)} files and {len(dbs)} SQLite files, {size / gb:.1f} GB uncompressed,"
          f" archive about {est / gb:.1f} GB")  # fmt: skip
    print(f"free in {dest}: {free / gb:.1f} GB, pruned first: {freed / gb:.1f} GB,"
          f" after: about {after / gb:.1f} GB")  # fmt: skip
    for old in stay:
        print(f"{'would keep' if dry else 'keep'} {old}")
    for old in early:
        print(f"{'would delete' if dry else 'delete'} {old} ({old.stat().st_size / gb:.1f} GB)"
              f" once the {len(stay)} kept read back whole")  # fmt: skip
    if dry:
        print(f"would write {dest / name} with SQLite copies of: {', '.join(dbs) or 'none'}")
    if after < MIN_FREE:
        sys.exit(f"refusing: under {MIN_FREE // gb} GB would be left free")
    if dry:
        return
    if early:
        for old in stay:
            verify(old)  # raises on a bad archive, so nothing is deleted early
        for old in early:
            old.unlink()
            print(f"deleted {old}")
    part = dest / (name + ".part")
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        with tarfile.open(part, "w:gz") as tar:
            for rel in dbs:
                copy = Path(tmp) / rel.replace("/", "__")
                src, out = sqlite3.connect(str(root / rel), timeout=30), sqlite3.connect(str(copy))
                try:
                    src.backup(out)
                finally:
                    src.close()
                    out.close()
                tar.add(copy, arcname=rel)
            for rel in files:
                tar.add(root / rel, arcname=rel, recursive=False)
    verify(part)
    final = part.rename(dest / name)
    uid, gid = (int(n) for n in owner.split(":"))
    os.chown(final, uid, gid)
    print(f"wrote and verified {final} ({final.stat().st_size / gb:.2f} GB)")
    for old in to_prune(dest, keep):
        old.unlink()
        print(f"deleted {old}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Back up the data volume slim.")
    ap.add_argument("label")
    ap.add_argument("--dest", default="/data")
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--volume", default="swarmify_data")
    ap.add_argument("--image", default="swarmify-api")
    ap.add_argument("--inside", metavar="UID:GID", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a.label):
        ap.error("label: letters, digits, dot, dash and underscore only")
    if a.keep < 1:
        ap.error("--keep must be 1 or more")
    if a.inside:
        inside(a.label, Path("/data"), Path("/out"), a.keep, a.dry_run, a.inside)
        return
    dest = Path(a.dest).resolve()
    script = Path(__file__).resolve()
    cmd = ["docker", "run", "--rm", "--entrypoint", "python",
           "-v", f"{a.volume}:/data", "-v", f"{dest}:/out", "-v", f"{script}:/backup.py:ro",
           a.image, "/backup.py", a.label, "--keep", str(a.keep),
           "--inside", f"{os.getuid()}:{os.getgid()}"]  # fmt: skip
    sys.exit(subprocess.call(cmd + (["--dry-run"] if a.dry_run else [])))


if __name__ == "__main__":
    main()
