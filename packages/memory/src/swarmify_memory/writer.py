"""GraphWriter: the single writer of every world's graph (ADR 0003).

It watches the world folders by polling a snapshot of their files (the graph output folder and the
assets excluded), waits until a world has been quiet for DEBOUNCE seconds after its last change,
then runs graphify's headless extract with the Claude CLI backend named explicitly. One thread does
every rebuild in turn, so rebuilds are serialized per world and queued across worlds. A queued
world's snapshot is taken as its rebuild starts, so changes made while it waited are absorbed by
that one rebuild. Each rebuild appends `graph.rebuilt` (sizes, deltas, extraction seconds and
tokens) or `graph.failed` (error, seconds, and whether graphify's shrink guard refused it) to the
world's events.

graphify's shrink guard refuses a write when a file it re-extracted came out with fewer nodes than
it had. It re-extracts only changed (or never finished) files, so when nothing else went wrong
the shrink is confined to files whose new text says less, and the writer accepts it at once with
`--allow-partial` (`graph.rebuilt` then says `shrink_accepted`). A shrink beside failed chunks is
not accepted: that flag would mark the failed files done. Any failed rebuild is retried RETRY
seconds later, change or not: graphify caches each finished chunk, so a retry after a timeout
resumes where it stopped. A world whose last STALLED rebuilds all failed is `stalled()`, which the
Needs-you page shows. Accepting a shrink by hand appends `graph.shrink_accepted`, which the writer
polls for, so it works from another process too: the world is rebuilt at once with
`--allow-partial`, that once. A deleted world (its `world.deleted` event) is dropped from the
queue, and a rebuild it outlived records nothing and removes the folder graphify recreated.
"""

import fnmatch
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_memory.ports import ASSETS, GRAPH_OUT, OUTCOMES, graph_file, graph_size

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "__pycache__"})
"""Folders no graph needs and the watcher must not walk: one remotion build's node_modules alone is
some 20,000 files, and a world keeps one per run."""
GENERATED = ("build", "build.*", "frames", "stills", "shots", "shader-cache")
"""A stage's working folders, neither watched nor extracted: its Template's working copy and an
earlier copy kept beside it (code, not knowledge), and what a render makes on the way to its
video (per-frame PNGs, contact-sheet stills, per-shot cuts, the GPU's shader cache), thousands
of images per run. The stage's artifacts stay in."""
DEBOUNCE = 30.0
"""Seconds a world must be quiet after its last file change before it is rebuilt."""
POLL = 2.0
EXTRACT_SECONDS = 3600.0
RETRY = 3600.0
"""Seconds after a failed rebuild before the writer tries again, with or without a change."""
# ponytail: a fixed retry; back off if a world that always fails crowds out the others.
STALLED = 3
"""Failed rebuilds in a row after which a world's memory is stalled and the operator is told."""
_UNSAFE = re.compile(
    r"chunk \d+/\d+ failed|produced no nodes|semantic extraction failed|all semantic chunks failed"
)
"""graphify's words for an incomplete extraction other than a shrink: chunks that failed or came
back hollow. `--allow-partial` would mark their files done, so their nodes would never come."""

Snapshot = dict[str, tuple[float, int]]
Usage = dict[str, int]
Extract = Callable[[Path, bool], Usage | None]
"""Rebuild the graph of the world at the folder, overriding the shrink guard when told to.
Returns the model tokens it spent."""
_TOKENS = re.compile(r"tokens: ([\d,]+) in / ([\d,]+) out")


class ShrinkRefused(RuntimeError):
    """graphify's shrink guard refused to write a graph smaller than the one it has. `confined`
    when the shrink was all that went wrong, so it is limited to the files it re-extracted."""

    def __init__(self, message: str, confined: bool = False) -> None:
        super().__init__(message)
        self.confined = confined


@dataclass(frozen=True)
class Stalled:
    """A world whose last `failures` rebuilds failed, from `since` to `at`; `error` is the last."""

    world_id: str
    failures: int
    since: str
    at: str
    error: str


def extract_command(folder: Path, allow_shrink: bool = False) -> list[str]:
    """graphify never auto-detects the Claude CLI backend, so it is named here (research note on
    graphify internals). graphify also skips graphify-out itself; the explicit exclude keeps
    that true if its defaults change. The assets are files, not knowledge."""
    return [
        sys.executable,
        "-m",
        "graphify",
        "extract",
        str(folder),
        "--backend",
        "claude-cli",
        "--exclude",
        f"{GRAPH_OUT}/",
        "--exclude",
        f"{ASSETS}/",
        *(a for d in GENERATED for a in ("--exclude", f"runs/*/*/{d}/")),
        "--exclude",
        f"*{OUTCOMES}",
        *(["--allow-partial"] if allow_shrink else []),
    ]


def run_graphify(folder: Path, allow_shrink: bool = False) -> Usage:
    done = subprocess.run(
        extract_command(folder, allow_shrink),
        capture_output=True,
        text=True,
        timeout=EXTRACT_SECONDS,
    )
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-5:]
        message = f"graphify extract exited {done.returncode}: {' | '.join(tail)}"
        # Only a semantic shrink is worth accepting. Failed chunks print the same --allow-partial
        # hint, but a retry heals them, and accepting would drop their nodes for good.
        said = done.stderr + done.stdout
        if "unverified semantic shrink" in said:
            raise ShrinkRefused(message, confined=not _UNSAFE.search(said))
        raise RuntimeError(message)
    found = _TOKENS.search(done.stdout)
    tokens = [int(n.replace(",", "")) for n in found.groups()] if found else [0, 0]
    return {"input_tokens": tokens[0], "output_tokens": tokens[1]}


def snapshot(folder: Path) -> Snapshot:
    """Every file's (mtime, size) but outcome sidecars, pruning the graph output, the assets, run
    GENERATED folders and SKIP_DIRS before descending, so the poll stays cheap however many builds
    and renders a world keeps."""
    # ponytail: a stat walk per world per poll; switch to FSEvents/inotify (watchdog) when a
    # world's knowledge itself reaches tens of thousands of files.
    files: Snapshot = {}
    for root, dirs, names in os.walk(folder):
        rel = Path(root).relative_to(folder)
        dirs[:] = [d for d in dirs if not _skipped(rel / d)]
        for name in names:
            if name.endswith(OUTCOMES):
                continue
            stat = (Path(root) / name).stat()
            files[str(rel / name)] = (stat.st_mtime, stat.st_size)
    return files


def _skipped(rel: Path) -> bool:
    parts = rel.parts
    return (
        rel.name in SKIP_DIRS
        or parts in ((GRAPH_OUT,), (ASSETS,))
        or (
            len(parts) == 4
            and parts[0] == "runs"
            and any(fnmatch.fnmatchcase(parts[3], d) for d in GENERATED)
        )
    )


class GraphWriter:
    def __init__(
        self,
        db: Path,
        worlds_dir: Path,
        extract: Extract = run_graphify,
        debounce: float = DEBOUNCE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.worlds_dir = worlds_dir
        self.extract = extract
        self.debounce = debounce
        self.clock = clock
        self._seen: dict[str, Snapshot] = {}
        self._changed: dict[str, float] = {}
        self._allow: set[str] = set()
        self._retry: dict[str, float] = {}
        self._accepted_seq = 0

    def accept_shrink(self, world_id: str) -> Event:
        """Ask for the world to be rebuilt once over graphify's shrink guard."""
        event = Event.new("graph.shrink_accepted", world_id=world_id, payload={})
        with closing(connect(self.db)) as conn:
            return append(conn, event)

    def tick(self) -> list[Event]:
        """One poll: note changed worlds, then rebuild every world quiet for `debounce`."""
        self._scan()
        self._accepted()
        now = self.clock()
        for world_id, at in list(self._retry.items()):
            if now >= at:
                del self._retry[world_id]
                self._changed.setdefault(world_id, now - self.debounce)
        due = sorted(w for w, at in self._changed.items() if now - at >= self.debounce)
        events = []
        for world_id in due:
            del self._changed[world_id]
            if (event := self.rebuild(world_id)) is not None:
                events.append(event)
        return events

    def rebuild(self, world_id: str) -> Event | None:
        """Rebuild the world's graph and record it; None when the world is or gets deleted."""
        folder = self.worlds_dir / world_id
        with closing(connect(self.db)) as conn:
            if _deleted(conn, world_id):
                return None
        nodes, edges = graph_size(folder)
        if folder.is_dir():  # what this rebuild reads; later changes need another
            self._seen[world_id] = snapshot(folder)
        allow = world_id in self._allow
        self._allow.discard(world_id)
        started, accepted = time.monotonic(), False
        try:
            try:
                usage = self.extract(folder, allow) or {}
            except ShrinkRefused as exc:
                if allow or not exc.confined:
                    raise
                log.info(
                    "rebuild of %s shrank only re-extracted files, accepting: %s", world_id, exc
                )
                usage, accepted = self.extract(folder, True) or {}, True
        except Exception as exc:
            log.warning("rebuild of %s failed: %s", world_id, exc)
            self._retry[world_id] = self.clock() + RETRY
            payload = {
                "error": str(exc),
                "shrink": isinstance(exc, ShrinkRefused),
                "seconds": round(time.monotonic() - started, 1),
            }
            event = Event.new("graph.failed", world_id=world_id, payload=payload)
        else:
            self._retry.pop(world_id, None)
            after_nodes, after_edges = graph_size(folder)
            payload = {
                "nodes": after_nodes,
                "edges": after_edges,
                "nodes_added": after_nodes - nodes,
                "edges_added": after_edges - edges,
                "seconds": round(time.monotonic() - started, 1),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
            if accepted:
                payload["shrink_accepted"] = True
            event = Event.new("graph.rebuilt", world_id=world_id, payload=payload)
        with closing(connect(self.db)) as conn:
            # Under the write lock Worlds.delete takes too: either the delete comes after this
            # event and removes the folder itself, or it came first and the folder goes here.
            conn.execute("BEGIN IMMEDIATE")
            deleted = _deleted(conn, world_id)
            stored = None if deleted else append(conn, event)
            conn.execute("COMMIT")
        if deleted:
            shutil.rmtree(folder, ignore_errors=True)
        return stored

    def stalled(self) -> list[Stalled]:
        """Every world whose last STALLED or more rebuilds failed, newest failure last read."""
        out = []
        with closing(connect(self.db)) as conn:
            for folder in sorted(p for p in self.worlds_dir.glob("*") if p.is_dir()):
                rows = conn.execute(
                    "SELECT type, ts, payload FROM events WHERE world_id = ?"
                    " AND type IN ('graph.rebuilt', 'graph.failed') ORDER BY seq DESC LIMIT 100",
                    (folder.name,),
                ).fetchall()
                failed = list(itertools.takewhile(lambda r: r[0] == "graph.failed", rows))
                if len(failed) >= STALLED:
                    error = str(json.loads(failed[0][2]).get("error", ""))
                    out.append(
                        Stalled(folder.name, len(failed), failed[-1][1], failed[0][1], error)
                    )
        return out

    def run(self, stop: threading.Event, poll: float = POLL) -> None:
        """Poll until `stop` is set. A failing poll is logged and retried next time."""
        while not stop.wait(poll):
            try:
                self.tick()
            except Exception:
                log.exception("graph writer poll failed, retrying")

    def _accepted(self) -> None:
        """Queue, due now, every world with a shrink acceptance no rebuild has followed yet."""
        with closing(connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT seq, world_id FROM events e WHERE seq > ?"
                " AND type = 'graph.shrink_accepted' AND NOT EXISTS (SELECT 1 FROM events f"
                " WHERE f.world_id = e.world_id AND f.seq > e.seq"
                " AND f.type IN ('graph.rebuilt', 'graph.failed'))",
                (self._accepted_seq,),
            ).fetchall()
        for seq, world_id in rows:
            self._accepted_seq = max(self._accepted_seq, seq)
            if (self.worlds_dir / world_id).is_dir():
                self._allow.add(world_id)
                self._changed[world_id] = self.clock() - self.debounce

    def _scan(self) -> None:
        now = self.clock()
        current = {p.name for p in self.worlds_dir.glob("*") if p.is_dir()}
        for gone in set(self._seen) - current:
            self._seen.pop(gone)
            self._changed.pop(gone, None)
            self._retry.pop(gone, None)
        for world_id in sorted(current):
            folder = self.worlds_dir / world_id
            snap = snapshot(folder)
            before = self._seen.get(world_id)
            if before is None:
                # First sight, at start-up: rebuild if any file is newer than the graph.
                built = graph_file(folder).stat().st_mtime if graph_file(folder).exists() else 0
                stale = any(mtime > built for mtime, _ in snap.values())
            else:
                stale = snap != before
            if stale:
                self._changed[world_id] = now
            self._seen[world_id] = snap


def _deleted(conn: sqlite3.Connection, world_id: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM events WHERE world_id = ? AND type = 'world.deleted'", (world_id,)
    )
    return found.fetchone() is not None


def main() -> None:
    """`swarmify-graph-writer`: the GraphWriter as its own process over `SWARMIFY_DATA_DIR`, for
    hosts that run it beside the API (which then sets SWARMIFY_GRAPH_WRITER=external)."""
    import os

    logging.basicConfig(level=logging.INFO)
    root = Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))
    (root / "worlds").mkdir(parents=True, exist_ok=True)
    GraphWriter(root / "swarmify.db", root / "worlds").run(threading.Event())
