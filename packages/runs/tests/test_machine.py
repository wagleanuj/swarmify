"""The stage state machine, driven by Hypothesis rule-based state machines."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from swarmify_runs.machine import (
    MAX_SEND_BACKS,
    TERMINAL,
    Aborted,
    BudgetAdded,
    BudgetSpent,
    Command,
    GateAnswered,
    GateRaised,
    Illegal,
    OpenGate,
    Paused,
    Rerun,
    Resumed,
    RunEvent,
    RunStage,
    RunState,
    SentBack,
    Skip,
    StageFailed,
    StageFinished,
    Started,
    transition,
)

KINDS = st.lists(
    st.sampled_from(["swarm", "gate", "connector", "workflow", "retro"]), min_size=1, max_size=6
)
INDEX = st.integers(min_value=-1, max_value=7)
EVENTS = st.one_of(
    st.just(Started()),
    st.builds(StageFinished, INDEX),
    st.builds(StageFailed, INDEX),
    st.builds(GateAnswered, INDEX),
    st.builds(SentBack, INDEX, INDEX),
    st.builds(BudgetSpent, INDEX),
    st.builds(GateRaised, INDEX),
    st.just(BudgetAdded()),
    st.builds(Rerun, INDEX),
    st.builds(Skip, INDEX),
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
    def send(self, event: RunEvent) -> None:
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
        # Any stage may be skipped (a Skip, or the stages a failure passes over to the retro);
        # the rest are done, then at most a failure and its retro, then pending.
        kept = [(i, s) for i, s in enumerate(self.state.stages) if s != "skipped"]
        stages = [s for _, s in kept]
        settled = [n for n, s in enumerate(stages) if s != "pending"]
        if settled:
            last = settled[-1]
            failed = stages.index("failed") if "failed" in stages[:last] else last
            assert all(s == "done" for s in stages[:failed])
            assert all(self.kinds[kept[n][0]] == "retro" for n in range(failed + 1, last))
            assert all(s == "pending" for s in stages[last + 1 :])

    @invariant()
    def status_agrees_with_stages(self) -> None:
        status, stages = self.state.status, self.state.stages
        if status == "queued":
            assert set(stages) <= {"pending", "skipped"}
        if status == "waiting_gate":
            assert "waiting" in stages
        if status == "running":
            assert "running" in stages
        if status == "done":
            assert set(stages) <= {"done", "skipped"}
        if status == "failed":
            assert "failed" in stages
        if "waiting" in stages:
            assert status in ("waiting_gate", "paused")

    @invariant()
    def send_backs_only_count_up(self) -> None:
        assert self.state.send_backs >= 0

    @invariant()
    def commands_point_at_the_active_stage(self) -> None:
        assert len(self.commands) <= 1
        for cmd in self.commands:
            expected = "waiting" if isinstance(cmd, OpenGate) else "running"
            assert self.state.stages[cmd.index] == expected
            # A gate stage only ever opens a gate; any other stage may raise one (GateRaised).
            if self.kinds[cmd.index] == "gate":
                assert isinstance(cmd, OpenGate)

    @invariant()
    def over_budget_means_a_stage_waits_for_money(self) -> None:
        if self.state.over_budget:
            assert "waiting" in self.state.stages and self.state.status not in TERMINAL


TestAnyEvents = AnyEvents.TestCase


class Shell(RuleBasedStateMachine):
    """Play the shell: answer only the outstanding command, sometimes pausing in between. The
    run must always end done with every stage done, each stage started exactly once."""

    @initialize(kinds=KINDS)
    def setup(self, kinds: list[str]) -> None:
        self.kinds = tuple(kinds)
        self.state, self.pending = transition(self.kinds, RunState.new(len(kinds)), Started())
        self.started = [c.index for c in self.pending]
        self.skipped: set[int] = set()

    def _apply(self, event: RunEvent) -> None:
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
    def budget_spent_then_added(self) -> None:
        """A stage stopped by the Budget runs again once money is added: it starts twice."""
        if self.pending and not isinstance(self.pending[0], OpenGate):
            index = self.pending.pop(0).index
            self._apply(BudgetSpent(index))
            assert self.state.status == "waiting_gate" and self.state.over_budget
            self.pending.pop(0)  # the budget gate
            self.started = [i for i in self.started if i != index]  # it starts afresh
            self._apply(BudgetAdded())
            assert not self.state.over_budget

    @rule()
    def pause_then_resume(self) -> None:
        if self.state.status in ("running", "waiting_gate"):
            self._apply(Paused())
            assert self.state.status == "paused"
            self.complete()
            self._apply(Resumed())

    @rule(data=st.data())
    def skip_a_stage_to_come(self, data: st.DataObject) -> None:
        """A skipped stage never starts, and the run still ends done."""
        pending = [i for i, s in enumerate(self.state.stages) if s == "pending"]
        if pending:
            index = data.draw(st.sampled_from(pending))
            self._apply(Skip(index))
            self.skipped.add(index)

    @rule(data=st.data(), paused=st.booleans())
    def rerun_from_a_finished_stage(self, data: st.DataObject, paused: bool) -> None:
        """A Rerun at a gate (or once paused there) starts the stage and every later one again,
        from the answered gate right before it if there is one (ADR 0007); it never touches a
        stage at work."""
        if not (self.pending and isinstance(self.pending[0], OpenGate)):
            return
        done = [i for i, s in enumerate(self.state.stages) if s == "done"]
        if not done:
            return
        to = data.draw(st.sampled_from(done))
        start = to - 1 if to and self.kinds[to - 1] == "gate" and to - 1 in done else to
        if paused:
            self._apply(Paused())
        self.pending.pop(0)  # the gate is no longer waiting
        self.started = [i for i in self.started if i < start]  # they start afresh
        self._apply(Rerun(to))
        assert "running" not in self.state.stages or not paused
        assert all(s != "done" for s in self.state.stages[start:])
        if paused:
            self._apply(Resumed())

    @invariant()
    def no_stage_starts_twice(self) -> None:
        assert len(self.started) == len(set(self.started))

    def teardown(self) -> None:
        while self.pending:
            self.complete()
        assert self.state.status == "done"
        expected = [i for i in range(len(self.kinds)) if i not in self.skipped]
        assert sorted(self.started) == expected


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


def test_a_failed_run_still_runs_its_retro_then_ends_failed() -> None:
    kinds = ("swarm", "gate", "connector", "retro")
    state, _ = transition(kinds, RunState.new(4), Started())
    state, cmds = transition(kinds, state, StageFailed(0))
    assert state == RunState("running", ("failed", "skipped", "skipped", "running"))
    assert cmds == [RunStage(3)]
    with pytest.raises(Illegal, match="failed run"):
        transition(kinds, transition(kinds, state, GateRaised(3))[0], SentBack(3, 1))
    ended, cmds = transition(kinds, state, StageFinished(3))
    assert ended == RunState("failed", ("failed", "skipped", "skipped", "done")) and cmds == []
    waiting, cmds = transition(kinds, state, GateRaised(3))
    assert waiting.status == "waiting_gate" and cmds == [OpenGate(3)]
    assert transition(kinds, waiting, GateAnswered(3))[0] == ended
    assert transition(kinds, state, StageFailed(3))[0] == RunState(
        "failed", ("failed", "skipped", "skipped", "failed")
    )


def test_abort_fails_the_active_stage() -> None:
    kinds = ("gate",)
    state, _ = transition(kinds, RunState.new(1), Started())
    assert transition(kinds, state, Aborted())[0] == RunState("aborted", ("failed",))


def test_kinds_must_match_stages() -> None:
    with pytest.raises(ValueError):
        transition(("gate",), RunState.new(2), Started())


@given(KINDS)
def test_send_back_reruns_from_the_named_stage_and_counts_the_visit(kinds: list[str]) -> None:
    """Drive the run to its first gate past stage 0 and send it back there, more often than
    the default limit: the machine counts visits, RunControl.decide holds each gate's limit."""
    kinds = ["connector", *kinds, "gate"]
    gate = kinds.index("gate", 1)
    k = tuple(kinds)
    state, cmds = transition(k, RunState.new(len(k)), Started())
    while not isinstance(cmds[0], OpenGate):
        state, cmds = transition(k, state, StageFinished(cmds[0].index))
    assert cmds == [OpenGate(gate)]

    for n in range(1, MAX_SEND_BACKS + 2):
        state, cmds = transition(k, state, SentBack(gate, 0))
        assert state.send_backs == n and state.status == "running"
        assert state.stages == ("running",) + ("pending",) * (len(k) - 1)
        assert cmds == [RunStage(0)]
        while not isinstance(cmds[0], OpenGate):
            state, cmds = transition(k, state, StageFinished(cmds[0].index))
    assert transition(k, state, GateAnswered(gate))[0].send_backs == MAX_SEND_BACKS + 1


def test_send_back_must_name_an_earlier_stage() -> None:
    kinds = ("gate", "connector")
    state, _ = transition(kinds, RunState.new(2), Started())
    for to in (0, 1, -1):
        with pytest.raises(Illegal):
            transition(kinds, state, SentBack(0, to))


def test_a_decision_while_paused_holds_until_resumed() -> None:
    kinds = ("connector", "gate")
    state, _ = transition(kinds, RunState.new(2), Started())
    state, _ = transition(kinds, state, StageFinished(0))
    state, _ = transition(kinds, state, Paused())
    held, cmds = transition(kinds, state, SentBack(1, 0))
    assert held == RunState("paused", ("pending", "pending"), 1) and cmds == []
    resumed, cmds = transition(kinds, held, Resumed())
    assert resumed == RunState("running", ("running", "pending"), 1) and cmds == [RunStage(0)]


def test_a_budget_gate_takes_money_or_an_abort_never_a_decision() -> None:
    kinds = ("swarm", "gate")
    state, _ = transition(kinds, RunState.new(2), Started())
    spent, cmds = transition(kinds, state, BudgetSpent(0))
    assert spent == RunState("waiting_gate", ("waiting", "pending"), over_budget=True)
    assert cmds == [OpenGate(0)]
    for event in (GateAnswered(0), SentBack(0, 0), BudgetSpent(0)):
        with pytest.raises(Illegal):
            transition(kinds, spent, event)
    again, cmds = transition(kinds, spent, BudgetAdded())
    assert again == RunState("running", ("running", "pending")) and cmds == [RunStage(0)]
    with pytest.raises(Illegal, match="not over budget"):
        transition(kinds, again, BudgetAdded())
    aborted, _ = transition(kinds, spent, Aborted())
    assert aborted == RunState("aborted", ("failed", "pending"))


def test_money_added_while_paused_runs_the_stage_on_resume() -> None:
    kinds = ("swarm",)
    state, _ = transition(kinds, RunState.new(1), Started())
    state, _ = transition(kinds, state, Paused())
    state, cmds = transition(kinds, state, BudgetSpent(0))
    assert state.status == "paused" and state.over_budget and cmds == [OpenGate(0)]
    state, cmds = transition(kinds, state, BudgetAdded())
    assert state == RunState("paused", ("pending",)) and cmds == []
    state, cmds = transition(kinds, state, Resumed())
    assert state == RunState("running", ("running",)) and cmds == [RunStage(0)]


def test_a_rerun_needs_a_finished_stage_and_a_run_at_rest() -> None:
    """From a gate or a pause it resets the stage and every later one, a skipped one staying
    skipped, and counts a visit; at work it is refused, and at a budget gate too."""
    kinds = ("swarm", "connector", "connector", "gate", "connector")
    state, _ = transition(kinds, RunState.new(5), Started())
    with pytest.raises(Illegal, match="pause first"):
        transition(kinds, state, Rerun(0))
    state, _ = transition(kinds, state, Skip(2))
    state, _ = transition(kinds, state, StageFinished(0))
    state, cmds = transition(kinds, state, StageFinished(1))
    assert state.stages == ("done", "done", "skipped", "waiting", "pending")
    assert cmds == [OpenGate(3)]
    with pytest.raises(Illegal, match="has not finished"):
        transition(kinds, state, Rerun(4))

    again, cmds = transition(kinds, state, Rerun(1))
    assert again == RunState("running", ("done", "running", "skipped", "pending", "pending"), 1)
    assert cmds == [RunStage(1)]
    paused, _ = transition(kinds, state, Paused())
    held, cmds = transition(kinds, paused, Rerun(0))
    assert held == RunState("paused", ("pending", "pending", "skipped", "pending", "pending"), 1)
    assert cmds == []
    paused_at_work, _ = transition(kinds, again, Paused())
    with pytest.raises(Illegal, match="pause first"):
        transition(kinds, paused_at_work, Rerun(0))
    spent, _ = transition(kinds, again, BudgetSpent(1))
    with pytest.raises(Illegal, match="budget gate"):
        transition(kinds, spent, Rerun(0))


def test_a_rerun_of_a_failed_stage_at_its_retro_gate_resets_what_the_failure_passed_over() -> None:
    """A stage a Skip passed over (`keep`) stays skipped; the failure's are pending again."""
    kinds = ("swarm", "connector", "connector", "connector", "retro")
    state, _ = transition(kinds, RunState.new(5), Started())
    state, _ = transition(kinds, state, Skip(3))
    state, _ = transition(kinds, state, StageFailed(0))
    state, _ = transition(kinds, state, GateRaised(4))
    assert state == RunState("waiting_gate", ("failed", "skipped", "skipped", "skipped", "waiting"))
    again, cmds = transition(kinds, state, Rerun(0, keep=(3,)))
    assert again.stages == ("running", "pending", "pending", "skipped", "pending")
    assert again.status == "running" and cmds == [RunStage(0)]


def test_a_skip_passes_over_a_stage_that_has_not_started() -> None:
    kinds = ("connector", "gate", "connector")
    state, _ = transition(kinds, RunState.new(3), Started())
    state, cmds = transition(kinds, state, Skip(1))
    assert state == RunState("running", ("running", "skipped", "pending")) and cmds == []
    with pytest.raises(Illegal, match="has started"):
        transition(kinds, state, Skip(0))
    state, cmds = transition(kinds, state, StageFinished(0))
    assert state.stages == ("done", "skipped", "running") and cmds == [RunStage(2)]
    state, _ = transition(kinds, state, StageFinished(2))
    assert state.status == "done"
