"""The Eval's pure parts: the task set, the output grader, pass at k and pass to the k, the blind
judge's prompt and pick, and the report that says whether the swarm wins."""

from collections import Counter
from math import comb

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_learning.evals import (
    TASKS,
    WORKFLOWS,
    EvalTask,
    Judgement,
    Trial,
    blind_order,
    grade,
    judge_prompt,
    made_by,
    markdown,
    parse_pick,
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


def test_twenty_or_more_tasks_spanning_four_workflows_each_with_a_unique_id() -> None:
    assert len(TASKS) >= 20
    assert len({t.id for t in TASKS}) == len(TASKS)
    assert all(t.keywords for t in TASKS)
    by_workflow = Counter(t.workflow for t in TASKS)
    assert (
        set(by_workflow)
        == set(WORKFLOWS)
        == {
            "Research brief",
            "Draft with approval",
            "Scheduled monitor",
            "Weekly short",
        }
    )
    assert by_workflow["Weekly short"] * 2 < len(TASKS), "the weekly short is not most of it"
    for task in TASKS:
        yaml = workflow_yaml(task, 2.5)
        assert task.topic in yaml and "budget_usd: 2.5" in yaml and "{" not in yaml
        assert "gate:" not in yaml and "schedule:" not in yaml and "connector:" not in yaml
    assert "produces: [script.md]" in workflow_yaml(TASK, 2.5)
    monitor = next(t for t in TASKS if t.workflow == "Scheduled monitor")
    assert "output_type: list[finding]" in workflow_yaml(monitor, 1)


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


def _on(topic: str, script: str) -> list[str]:
    task = next(t for t in TASKS if t.topic == topic)
    return grade(task, {"ideas.md": IDEAS, "script.md": script + " " + "word " * 60})


def test_the_grader_checks_topic_by_meaning_not_one_exact_keyword() -> None:
    # On topic in other words: a stem of a keyword, or a synonym the task lists.
    assert _on("urban cycling", "Biking downtown? Riders, lock it right.") == []
    assert _on("home cooking on a budget", "Pumpkin guts into a cheap dinner pot.") == []
    assert _on("learning Spanish", "Three words every Spain trip needs.") == []
    assert _on("running a first marathon", "Your first 42k starts with running slow.") == []
    # Off topic still fails, and a stem must match a whole word, not part of one.
    assert _on("urban cycling", "Five tips to sleep better tonight.") == [
        "script.md never names urban cycling"
    ]
    assert _on("astronomy from the backyard", "Start your morning with a stretch.") == [
        "script.md never names astronomy from the backyard"
    ]


def _first(workflow: str) -> EvalTask:
    return next(t for t in TASKS if t.workflow == workflow)


LINKS = " ".join(f"https://example.org/{n}" for n in range(3))


def test_a_research_brief_is_graded_on_length_links_and_topic() -> None:
    task = _first("Research brief")
    brief = f"{task.keywords[0]} " + "word " * 300 + LINKS
    assert grade(task, {"brief.md": brief}) == []
    assert grade(task, {}) == ["brief.md is missing"]
    assert grade(task, {"brief.md": "Too short. https://a.org https://a.org"}) == [
        "brief.md is 4 words, not 250 to 1000",
        "brief.md cites 1 links, not 3",
        f"brief.md never names {task.topic}",
    ]


def test_a_draft_needs_its_outline_and_a_draft_of_the_right_length_on_topic() -> None:
    task = _first("Draft with approval")
    draft = f"# Title\n\n{task.keywords[0]} " + "word " * 600
    assert grade(task, {"outline.md": "1. Intro", "draft.md": draft}) == []
    assert grade(task, {}) == ["outline.md is missing", "draft.md is missing"]
    assert grade(task, {"outline.md": "x", "draft.md": "word " * 1300}) == [
        "draft.md is 1300 words, not 500 to 1200",
        f"draft.md never names {task.topic}",
    ]


def test_a_monitor_is_graded_on_its_typed_list_of_findings() -> None:
    task = _first("Scheduled monitor")
    finding = f"# New rule\n\nWhat changed in {task.keywords[0]}. https://example.org/a"
    found = {f"finding-{n}.md": finding for n in range(3)}
    assert grade(task, {**found, "verdict-1.md": "passed"}) == []
    assert grade(task, {"finding-a.md": finding}) == ["1 findings, not 3 to 10"]
    many = {f"finding-{n}.md": finding for n in range(11)}
    assert grade(task, many) == ["11 findings, not 3 to 10"]
    bare = {f"finding-{n}.md": "Something else." for n in range(3)}
    assert grade(task, bare) == [
        "3 of 3 findings have no source link",
        f"no finding names {task.topic}",
    ]
    assert made_by(task, "finding-new-rule.md") and not made_by(task, "verdict-1.md")
    assert made_by(TASK, "ideas.md") and not made_by(TASK, "finding-a.md")


def test_the_judge_reads_every_item_of_a_typed_list_and_a_missing_list_says_so() -> None:
    task = _first("Scheduled monitor")
    outputs = {"swarm": {"finding-b.md": "B-ITEM", "finding-a.md": "A-ITEM"}}
    prompt = judge_prompt(task, outputs)
    assert task.topic in prompt and "typed list" in prompt
    swarm_label = "AB"[blind_order(task.id).index("swarm")]
    other = "B" if swarm_label == "A" else "A"
    assert prompt.index(f"## {swarm_label}: finding-a.md") < prompt.index("A-ITEM")
    assert prompt.index("A-ITEM") < prompt.index("B-ITEM")
    assert f"## {other}: finding-*\n\n(missing)" in prompt


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


def test_the_blind_order_is_shuffled_per_task_and_stable() -> None:
    orders = {t.id: blind_order(t.id) for t in TASKS}
    assert {o for o in orders.values()} == {("baseline", "swarm"), ("swarm", "baseline")}
    assert all(blind_order(t.id) == orders[t.id] for t in TASKS)


def test_the_judge_sees_both_arms_unlabelled_in_the_task_order_and_its_pick_maps_back() -> None:
    outputs = {
        "baseline": {"ideas.md": "IDEAS-ONE", "script.md": "SCRIPT-ONE"},
        "swarm": {"ideas.md": "IDEAS-TWO"},
    }
    prompt = judge_prompt(TASK, outputs)
    assert "baseline" not in prompt.lower() and "swarm" not in prompt.lower()
    first, second = blind_order(TASK.id)
    mark = {"baseline": "IDEAS-ONE", "swarm": "IDEAS-TWO"}
    a, b = prompt.index("## A: ideas.md"), prompt.index("## B: ideas.md")
    assert a < prompt.index(mark[first]) < b < prompt.index(mark[second])
    assert "## A: script.md" in prompt and "(missing)" in prompt and TASK.topic in prompt

    pick = parse_pick(TASK.id, 1, {"better": "A", "reason": "Sharper hook."})
    assert pick == Judgement(TASK.id, 1, first, "Sharper hook.")
    assert parse_pick(TASK.id, 0, {"better": "B", "reason": "x"}).winner == second
    assert parse_pick(TASK.id, 0, {"better": "tie", "reason": "Even."}).winner is None
    for bad in ({"better": "C", "reason": "x"}, {"better": "A", "reason": " "}, "A"):
        with pytest.raises(ValueError):
            parse_pick(TASK.id, 0, bad)


TRIALS = [
    trial("a", "baseline", 0, True, 0.5, 600),
    trial("a", "baseline", 1, False, 0.5, 600),
    trial("b", "baseline", 0, False, 0.4, 500),
    trial("b", "baseline", 1, False, 0.4, None),
    trial("a", "swarm", 0, True, 1.5, 300),
    trial("a", "swarm", 1, True, 1.5, 300),
    trial("b", "swarm", 0, True, 1.0, 200),
    trial("b", "swarm", 1, False, 1.0, 200),
]


def judged(*winners: str | None) -> list[Judgement]:
    return [Judgement("ab"[i // 2], i % 2, w, f"Pick {i}") for i, w in enumerate(winners)]


def test_the_report_keeps_acceptance_and_counts_the_judges_wins_losses_ties_and_cost_a_win() -> (
    None
):
    picks = judged("swarm", "swarm", None, "baseline")
    report = summarize(TRIALS, 2, "eval_1", "2026-10-27T10:00:00+00:00", picks)
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
    assert (swarm.wins, swarm.losses, swarm.ties) == (2, 1, 1)
    assert (baseline.wins, baseline.losses, baseline.ties) == (1, 2, 1)
    assert swarm.cost_per_win == pytest.approx(2.5) and baseline.cost_per_win == pytest.approx(1.8)
    assert report.tasks == 2 and report.swarm_wins and report.judgements == picks
    assert report.verdict == (
        "The swarm wins: the blind judge preferred it in 2 of 3 decided pairs (1 tied), at $2.50"
        " a win against $1.80 a win for the baseline. It accepted 75% of trials first try"
        " against 25%, in 4 min an accepted trial against 10 min, at $1.25 a trial against"
        " $0.45 (estimated)."
    )
    text = markdown(report)
    assert report.verdict in text
    assert "| swarm | 4 | 75% | 100% | 50% | $1.25 | 4 min | 2 | 1 | 1 | $2.50 a win |" in text
    assert "| b | baseline | 2 | done | $0.40 | none | script.md is missing |" in text
    assert "| b | 1 | tie | Pick 2 |" in text and "| a | 1 | swarm | Pick 0 |" in text


def test_the_swarm_wins_only_on_more_than_half_of_the_decided_pairs() -> None:
    def wins(*winners: str | None) -> bool:
        return summarize(TRIALS, 2, "eval_1", "now", judged(*winners)).swarm_wins

    assert wins("swarm", None, None, None), "ties are left out"
    assert not wins("swarm", "baseline", None, None), "half is not more than half"
    assert not wins(None, None, None, None) and not wins()
    assert not wins("baseline", "baseline", "swarm", None)
    lost = summarize(TRIALS, 2, "eval_2", "now", judged("baseline"))
    assert lost.verdict.startswith("The swarm does not win: the blind judge preferred it in 0 of 1")
    assert "at no win against $1.80 a win" in lost.verdict
    with pytest.raises(ValueError, match="unknown arm hooli"):
        summarize(TRIALS, 2, "eval_3", "now", judged("hooli"))


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
