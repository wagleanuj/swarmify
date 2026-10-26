"""The stage state machine, driven by Hypothesis rule-based state machines."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from swarmify_runs.machine import (
    TERMINAL,
    Aborted,
    Command,
    Event,
    GateAnswered,
    Illegal,
    OpenGate,
    Paused,
    Resumed,
    RunStage,
    RunState,
    StageFailed,
    StageFinished,
    Started,
    transition,
)

KINDS = st.lists(
    st.sampled_from(["swarm", "gate", "connector", "workflow"]), min_size=1, max_size=6
)
INDEX = st.integers(min_value=-1, max_value=7)
EVENTS = st.one_of(
    st.just(Started()),
    st.builds(StageFinished, INDEX),
    st.builds(StageFailed, INDEX),
    st.builds(GateAnswered, INDEX),
    st.just(Paused()),
    st.just(Resumed()),
    st.just(Aborted()),
)


class AnyEvents(RuleBasedStateMachine):
    """Throw any event at the machine; illegal ones raise and change nothing, legal ones keep
    every invariant."""

    @initialize(kinds=KINDS)
    def setup(self, kinds: list[str]) -> None:
        self.kinds = tuple(kinds)
        self.state = RunState.new(len(kinds))
        self.commands: list[Command] = []

    @rule(event=EVENTS)
    def send(self, event: Event) -> None:
        before = self.state
        try:
            self.state, self.commands = transition(self.kinds, self.state, event)
        except Illegal:
            assert self.state == before
            return
        assert before.status not in TERMINAL

    @invariant()
    def at_most_one_active_stage(self) -> None:
        active = [s for s in self.state.stages if s in ("running", "waiting")]
        assert len(active) <= 1

    @invariant()
    def stages_form_done_prefix_then_pending(self) -> None:
        stages = list(self.state.stages)
        settled = [i for i, s in enumerate(stages) if s != "pending"]
        if settled:
            last = settled[-1]
            assert all(s == "done" for s in stages[:last])
            assert all(s == "pending" for s in stages[last + 1 :])

    @invariant()
    def status_agrees_with_stages(self) -> None:
        status, stages = self.state.status, self.state.stages
        if status == "queued":
            assert set(stages) == {"pending"}
        if status == "waiting_gate":
            assert "waiting" in stages
        if status == "running":
            assert "running" in stages
        if status == "done":
            assert set(stages) == {"done"}
        if status == "failed":
            assert "failed" in stages
        if "waiting" in stages:
            assert status in ("waiting_gate", "paused")

    @invariant()
    def commands_point_at_the_active_stage(self) -> None:
        assert len(self.commands) <= 1
        for cmd in self.commands:
            expected = "waiting" if isinstance(cmd, OpenGate) else "running"
            assert self.state.stages[cmd.index] == expected
            assert (self.kinds[cmd.index] == "gate") == isinstance(cmd, OpenGate)


TestAnyEvents = AnyEvents.TestCase


class Shell(RuleBasedStateMachine):
    """Play the shell: answer only the outstanding command, sometimes pausing in between. The
    run must always end done with every stage done, each stage started exactly once."""

    @initialize(kinds=KINDS)
    def setup(self, kinds: list[str]) -> None:
        self.kinds = tuple(kinds)
        self.state, self.pending = transition(self.kinds, RunState.new(len(kinds)), Started())
        self.started = [c.index for c in self.pending]

    def _apply(self, event: Event) -> None:
        self.state, commands = transition(self.kinds, self.state, event)
        self.pending += commands
        self.started += [c.index for c in commands]

    @rule()
    def complete(self) -> None:
        if self.pending:
            cmd = self.pending.pop(0)
            self._apply(
                GateAnswered(cmd.index) if isinstance(cmd, OpenGate) else StageFinished(cmd.index)
            )

    @rule()
    def pause_then_resume(self) -> None:
        if self.state.status in ("running", "waiting_gate"):
            self._apply(Paused())
            assert self.state.status == "paused"
            self.complete()
            self._apply(Resumed())

    @invariant()
    def no_stage_starts_twice(self) -> None:
        assert len(self.started) == len(set(self.started))

    def teardown(self) -> None:
        while self.pending:
            self.complete()
        assert self.state.status == "done"
        assert sorted(self.started) == list(range(len(self.kinds)))


TestShell = Shell.TestCase


@given(KINDS)
def test_a_gate_waits_and_other_stages_run(kinds: list[str]) -> None:
    state, cmds = transition(tuple(kinds), RunState.new(len(kinds)), Started())
    expected = OpenGate(0) if kinds[0] == "gate" else RunStage(0)
    assert cmds == [expected]
    assert state.status == ("waiting_gate" if kinds[0] == "gate" else "running")


def test_failure_ends_the_run_and_leaves_later_stages_pending() -> None:
    kinds = ("connector", "gate")
    state, _ = transition(kinds, RunState.new(2), Started())
    state, cmds = transition(kinds, state, StageFailed(0))
    assert state == RunState("failed", ("failed", "pending")) and cmds == []
    with pytest.raises(Illegal):
        transition(kinds, state, Resumed())


def test_abort_fails_the_active_stage() -> None:
    kinds = ("gate",)
    state, _ = transition(kinds, RunState.new(1), Started())
    assert transition(kinds, state, Aborted())[0] == RunState("aborted", ("failed",))


def test_kinds_must_match_stages() -> None:
    with pytest.raises(ValueError):
        transition(("gate",), RunState.new(2), Started())
