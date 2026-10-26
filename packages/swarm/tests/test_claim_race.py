"""Concurrent claims from real parallel SQLite connections: exactly one winner, never a mock."""

import multiprocessing as mp
import threading
from pathlib import Path
from typing import Any

from swarmify_swarm import OPERATOR, Refused, SqliteStorage

TASKS = 5


def _claim(db: str, task_id: str, agent: str, barrier: Any) -> str | None:
    """Claim on a fresh connection once every contender is lined up. The agent name if won."""
    board = SqliteStorage(Path(db))
    barrier.wait()
    try:
        return board.claim(task_id, agent).owner
    except Refused:
        return None


def _seed(board: SqliteStorage) -> list[str]:
    return [
        board.add_task("world_1", "run_1", "stage_1", f"task {i}", added_by=OPERATOR).id
        for i in range(TASKS)
    ]


def _race_in_process(db: str, task_id: str, agent: str, barrier: Any, results: Any) -> None:
    results.put(_claim(db, task_id, agent, barrier))


def test_threads_racing_for_a_task_yield_one_winner(db: Path, board: SqliteStorage) -> None:
    agents = 16
    for task_id in _seed(board):
        barrier = threading.Barrier(agents)
        results: list[str | None] = [None] * agents

        def run(i: int, task_id: str, barrier: threading.Barrier, out: list[str | None]) -> None:
            out[i] = _claim(str(db), task_id, f"agent{i}", barrier)

        threads = [
            threading.Thread(target=run, args=(i, task_id, barrier, results)) for i in range(agents)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert board.get_task(task_id).owner == winners[0]  # type: ignore[union-attr]


def test_processes_racing_for_a_task_yield_one_winner(db: Path, board: SqliteStorage) -> None:
    ctx = mp.get_context("spawn")
    agents = 6
    for task_id in _seed(board)[:2]:
        barrier, results = ctx.Barrier(agents), ctx.Queue()
        procs = [
            ctx.Process(
                target=_race_in_process, args=(str(db), task_id, f"agent{i}", barrier, results)
            )
            for i in range(agents)
        ]
        for p in procs:
            p.start()
        outcomes = [results.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0
        winners = [r for r in outcomes if r is not None]
        assert len(winners) == 1
        assert board.get_task(task_id).owner == winners[0]  # type: ignore[union-attr]
