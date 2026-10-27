"""The Eval's pure parts: the task set, the output grader, pass at k and pass to the k, and the
report that says whether the swarm wins."""

from math import comb

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_learning.evals import (
    TASKS,
    Trial,
    grade,
    markdown,
    pass_at_k,
    pass_pow_k,
    summarize,
    workflow_yaml,
)

TASK = TASKS[0]
IDEAS = "\n".join(f"{n}. Idea {n}: a hook" for n in range(1, 6))
SCRIPT = "Hook: " + " ".join(["word"] * 100) + f" {TASK.keywords[0]}."


def trial(task: str, arm: str, index: int, ok: bool, cost: float, took: float | None) -> Trial:
    failed = [] if ok else ["script.md is missing"]
    return Trial(task, arm, index, "world_1", "run_1", "done", cost, took, failed)


def test_twenty_or_more_tasks_each_with_a_unique_id_and_a_valid_workflow() -> None:
    assert len(TASKS) >= 20
    assert len({t.id for t in TASKS}) == len(TASKS)
    assert all(t.keywords for t in TASKS)
    yaml = workflow_yaml(TASK, 2.5)
    assert TASK.topic in yaml and "budget_usd: 2.5" in yaml and "produces: [script.md]" in yaml


def test_the_grader_reads_the_outputs_only() -> None:
    assert grade(TASK, {"ideas.md": IDEAS, "script.md": SCRIPT}) == []
    assert grade(TASK, {}) == ["ideas.md is missing", "script.md is missing"]
    few = grade(TASK, {"ideas.md": "1. One\n2. Two", "script.md": "Too short."})
    assert few == [
        "ideas.md ranks 2 ideas, not 5",
        "script.md is 2 words, not 60 to 200",
        f"script.md never names {TASK.topic}",
    ]
    long = "word " * 250 + TASK.keywords[0].upper()
    assert grade(TASK, {"ideas.md": IDEAS, "script.md": long}) == [
        "script.md is 251 words, not 60 to 200"
    ]


def test_pass_at_k_and_pass_to_the_k() -> None:
    assert pass_at_k(3, 0, 3) == 0 and pass_at_k(3, 1, 3) == 1
    assert pass_pow_k(3, 2, 3) == 0 and pass_pow_k(3, 3, 3) == 1
    assert pass_at_k(4, 2, 2) == pytest.approx(5 / 6)
    assert pass_pow_k(4, 2, 2) == pytest.approx(1 / 6)


@given(
    st.integers(1, 12).flatmap(
        lambda n: st.tuples(st.just(n), st.integers(0, n), st.integers(1, n))
    )
)
def test_pass_to_the_k_never_exceeds_pass_at_k(nck: tuple[int, int, int]) -> None:
    n, c, k = nck
    eps = 1e-12
    assert 0 <= pass_pow_k(n, c, k) <= c / n + eps and c / n <= pass_at_k(n, c, k) + eps <= 1 + eps
    assert pass_at_k(n, c, k) == pytest.approx(1 - comb(n - c, k) / comb(n, k))


def test_the_report_compares_cost_duration_and_acceptance_and_names_the_winner() -> None:
    trials = [
        trial("a", "baseline", 0, True, 0.5, 600),
        trial("a", "baseline", 1, False, 0.5, 600),
        trial("b", "baseline", 0, False, 0.4, 500),
        trial("b", "baseline", 1, False, 0.4, None),
        trial("a", "swarm", 0, True, 1.5, 300),
        trial("a", "swarm", 1, True, 1.5, 300),
        trial("b", "swarm", 0, True, 1.0, 200),
        trial("b", "swarm", 1, False, 1.0, 200),
    ]
    report = summarize(trials, 2, "eval_1", "2026-10-27T10:00:00+00:00")
    baseline, swarm = report.arms
    assert (baseline.arm, baseline.trials, baseline.accepted) == ("baseline", 4, 1)
    assert (baseline.pass_at_k, baseline.pass_pow_k) == (0.5, 0.0)
    assert baseline.cost_usd == pytest.approx(0.45) and baseline.duration_s == 600
    assert (swarm.accepted, swarm.acceptance, swarm.pass_at_k, swarm.pass_pow_k) == (
        3,
        0.75,
        1,
        0.5,
    )
    assert report.tasks == 2 and report.swarm_wins
    assert report.verdict == (
        "The swarm wins: it accepted 75% of trials against 25% for the baseline, in 4 min an"
        " accepted trial against 10 min, at $1.25 a trial against $0.45 (estimated)."
    )
    text = markdown(report)
    assert report.verdict in text and "| swarm | 4 | 75% | 100% | 50% | $1.25 | 4 min |" in text
    assert "| b | baseline | 2 | done | $0.40 | none | script.md is missing |" in text

    slower = [
        t if t.arm == "baseline" else trial(t.task_id, "swarm", t.index, True, 1, 900)
        for t in trials
    ]
    lost = summarize(slower, 2, "eval_2", "2026-10-27T10:00:00+00:00")
    assert not lost.swarm_wins and lost.verdict.startswith("The swarm does not win: ")

    never = [trial("a", "baseline", 0, False, 1, 100), trial("a", "swarm", 0, True, 1, 300)]
    assert summarize(never, 1, "eval_3", "now").swarm_wins, "a baseline that never accepts loses"


def test_a_report_needs_k_trials_of_each_arm_on_each_task() -> None:
    with pytest.raises(ValueError, match="b on swarm has 1 trials, fewer than k=2"):
        summarize(
            [
                trial("b", "baseline", 0, True, 1, 1),
                trial("b", "baseline", 1, True, 1, 1),
                trial("b", "swarm", 0, True, 1, 1),
            ],
            2,
            "eval_1",
            "now",
        )
    with pytest.raises(ValueError, match="unknown arm"):
        summarize([trial("b", "hooli", 0, True, 1, 1)], 1, "eval_1", "now")
