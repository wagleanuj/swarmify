"""GraphWriter: the single writer of every world's graph (ADR 0003).

It watches the world folders by polling a snapshot of their files (the graph output folder
excluded), waits until a world has been quiet for DEBOUNCE seconds after its last change, then
runs graphify's headless extract with the Claude CLI backend named explicitly. One thread does
every rebuild in turn, so rebuilds are serialized per world and queued across worlds. A queued
world's snapshot is taken as its rebuild starts, so changes made while it waited are absorbed by
that one rebuild. Each rebuild appends `graph.rebuilt` (sizes, deltas, extraction seconds and
tokens) or `graph.failed` (error, seconds, and whether graphify's shrink guard refused it) to the
world's events; a failed rebuild waits for the next change. Accepting a shrink appends
`graph.shrink_accepted`, which the writer polls for, so it works from another process too: the
world is rebuilt at once with graphify's `--allow-partial`, that once. A deleted world (its
`world.deleted` event) is dropped from the queue, and a rebuild it outlived records nothing and
removes the folder graphify recreated.
"""

import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from swarmify_kernel import Event
from swarmify_kernel.events import append
from swarmify_kernel.sqlite import connect
from swarmify_memory.ports import GRAPH_OUT, graph_file, graph_size

log = logging.getLogger(__name__)

DEBOUNCE = 30.0
"""Seconds a world must be quiet after its last file change before it is rebuilt."""
POLL = 2.0
EXTRACT_SECONDS = 3600.0

Snapshot = dict[str, tuple[float, int]]
Usage = dict[str, int]
Extract = Callable[[Path, bool], Usage | None]
"""Rebuild the graph of the world at the folder, overriding the shrink guard when told to.
Returns the model tokens it spent."""
_TOKENS = re.compile(r"tokens: ([\d,]+) in / ([\d,]+) out")


class ShrinkRefused(RuntimeError):
    """graphify's shrink guard refused to write a graph smaller than the one it has."""


def extract_command(folder: Path, allow_shrink: bool = False) -> list[str]:
    """graphify never auto-detects the Claude CLI backend, so it is named here (research note on
    graphify internals). graphify also skips graphify-out itself; the explicit exclude keeps
    that true if its defaults change."""
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
        # Every shrink guard refusal names the flag that overrides it.
        shrunk = "--allow-partial" in done.stderr + done.stdout
        raise (ShrinkRefused if shrunk else RuntimeError)(message)
    found = _TOKENS.search(done.stdout)
    tokens = [int(n.replace(",", "")) for n in found.groups()] if found else [0, 0]
    return {"input_tokens": tokens[0], "output_tokens": tokens[1]}


def snapshot(folder: Path) -> Snapshot:
    # ponytail: a full stat walk per world per poll; switch to FSEvents/inotify (watchdog) when a
    # world holds tens of thousands of files.
    out = folder / GRAPH_OUT
    files: Snapshot = {}
    for path in folder.rglob("*"):
        if path.is_file() and not path.is_relative_to(out):
            stat = path.stat()
            files[str(path.relative_to(folder))] = (stat.st_mtime, stat.st_size)
    return files


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
        started = time.monotonic()
        try:
            usage = self.extract(folder, allow) or {}
        except Exception as exc:
            log.warning("rebuild of %s failed: %s", world_id, exc)
            payload = {
                "error": str(exc),
                "shrink": isinstance(exc, ShrinkRefused),
                "seconds": round(time.monotonic() - started, 1),
            }
            event = Event.new("graph.failed", world_id=world_id, payload=payload)
        else:
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
