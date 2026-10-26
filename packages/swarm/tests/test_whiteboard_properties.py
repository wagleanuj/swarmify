"""Hypothesis rule-based machine over the whiteboard: its invariants hold for any sequence."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from swarm_db import migrated
from swarmify_swarm import OPERATOR, Refused, SqliteStorage

AGENTS = st.sampled_from(["gilfoyle", "dinesh", "jared"])


class Whiteboard(RuleBasedStateMachine):
    @initialize()
    def setup(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.board = SqliteStorage(migrated(Path(self.dir.name)))
        self.now = datetime(2026, 10, 26, tzinfo=UTC)
        self.ids: list[str] = []
        self.done: set[str] = set()
        self.expected: dict[str, str | None] = {}

    def teardown(self) -> None:
        if hasattr(self, "dir"):
            self.dir.cleanup()

    def _pick(self, i: int) -> str | None:
        return self.ids[i % len(self.ids)] if self.ids else None

    @rule(deps=st.lists(st.integers(0, 20), max_size=2))
    def add(self, deps: list[int]) -> None:
        depends = tuple(self.ids[d % len(self.ids)] for d in deps) if self.ids else ()
        task = self.board.add_task(
            "world_1", "run_1", "stage_1", "t", added_by=OPERATOR, depends_on=depends
        )
        self.ids.append(task.id)
        self.expected[task.id] = None

    @rule(i=st.integers(0, 20), agent=AGENTS)
    def claim(self, i: int, agent: str) -> None:
        if (task_id := self._pick(i)) is None:
            return
        try:
            self.board.claim(task_id, agent, now=self.now)
        except Refused:
            assert (
                self.expected[task_id] is not None or task_id in self.done or self._blocked(task_id)
            )
            return
        assert self.expected[task_id] is None and not self._blocked(task_id)
        self.expected[task_id] = agent

    @rule(i=st.integers(0, 20), agent=AGENTS, finish=st.booleans())
    def act(self, i: int, agent: str, finish: bool) -> None:
        if (task_id := self._pick(i)) is None:
            return
        holds = self.expected[task_id] == agent and task_id not in self.done
        try:
            if finish:
                self.board.finish(task_id, agent, note="n")
                self.done.add(task_id)
            else:
                self.board.heartbeat(task_id, agent, now=self.now)
        except Refused:
            assert not holds
            return
        assert holds

    @rule(minutes=st.integers(0, 3))
    def tick(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)
        for task in self.board.expire(now=self.now):
            assert task.id not in self.done
            self.expected[task.id] = None

    def _blocked(self, task_id: str) -> bool:
        task = self.board.get_task(task_id)
        assert task is not None
        return any(d not in self.done for d in task.depends_on)

    @invariant()
    def board_matches_the_model(self) -> None:
        if not hasattr(self, "board"):
            return
        for task in self.board.list_tasks("stage_1"):
            assert (task.status == "open") == (task.owner is None)
            assert (task.status == "done") == (task.id in self.done)
            assert task.owner == self.expected[task.id]
            assert (task.status == "claimed") == (task.heartbeat_at is not None)


Whiteboard.TestCase.settings = settings(max_examples=30, stateful_step_count=25, deadline=None)
TestWhiteboard = Whiteboard.TestCase
