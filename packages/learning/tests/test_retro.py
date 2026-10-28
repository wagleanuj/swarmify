"""The retro's pure parts: evidence and recurrences folded from events, the answer parsed."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swarmify_kernel import Event
from swarmify_learning import (
    RECURRENCE,
    Recurrence,
    check_skill,
    evidence,
    parse_retro,
    recurrences,
)
from swarmify_learning.retro import LOG_TAIL, frontmatter, prompt

SKILL = "---\nname: hooks\ndescription: Open on the hook.\n---\n\nWrite the hook first.\n"


def ev(type: str, payload: dict[str, Any], run_id: str | None = None) -> Event:
    return Event.new(type, world_id="world_1", payload=payload, run_id=run_id)


def run_event(run_id: str, decisions: list[dict], workflow_id: str = "wf_1", **over: Any) -> Event:
    stages = [
        {"id": f"{run_id}_s0", "name": "research", "type": "swarm"},
        {"id": f"{run_id}_s1", "name": "review", "type": "gate"},
    ]
    payload = {
        "id": run_id,
        "name": "Short",
        "workflow_id": workflow_id,
        "status": "done",
        "send_backs": sum(1 for d in decisions if d.get("send_back_to")),
        "stages": stages,
        "decisions": decisions,
        **over,
    }
    return ev("run.updated", payload, run_id)


def decision(stage: str, **over: Any) -> dict:
    return {"id": "dec", "stage_name": stage, "kind": "approve", "choice": None, "note": "", **over}


def test_evidence_folds_verdicts_decisions_and_operator_tasks_of_the_run() -> None:
    events = [
        run_event("run_1", [decision("review", send_back_to="research", note="Too long.")]),
        ev(
            "verdict.created",
            {
                "run_id": "run_1",
                "stage_id": "run_1_s0",
                "loop": 1,
                "passed": False,
                "reasons": ["thin"],
                "gaps": ["Find more"],
            },  # fmt: skip
            "run_1",
        ),
        ev(
            "verdict.created",
            {
                "run_id": "run_1",
                "stage_id": "run_1_s0",
                "loop": 2,
                "passed": True,
                "reasons": ["ok"],
                "gaps": [],
            },  # fmt: skip
            "run_1",
        ),
        ev(
            "task.updated",
            {
                "id": "t1",
                "run_id": "run_1",
                "stage_id": "run_1_s0",
                "title": "Check the source",
                "added_by": "operator",
            },
            "run_1",
        ),  # fmt: skip
        ev(
            "task.updated",
            {
                "id": "t1",
                "run_id": "run_1",
                "stage_id": "run_1_s0",
                "title": "Check the source",
                "added_by": "operator",
            },
            "run_1",
        ),  # fmt: skip
        ev(
            "task.updated",
            {
                "id": "t2",
                "run_id": "run_1",
                "stage_id": "run_1_s0",
                "title": "Planned",
                "added_by": "planner",
            },
            "run_1",
        ),  # fmt: skip
        run_event("run_2", [decision("review", kind="pick", choice="2")]),
        # The newest run.updated wins: the run ended later.
        run_event(
            "run_1",
            [decision("review", send_back_to="research", note="Too long.")],
            status="failed",
        ),  # fmt: skip
    ]
    e = evidence(events, "run_1")
    assert (e.name, e.status, e.send_backs, e.workflow_id) == ("Short", "failed", 1, "wf_1")
    assert e.verdicts == [
        "research loop 1: failed, because thin (gaps: Find more)",
        "research loop 2: passed, because ok",
    ]
    assert e.decisions == ["at review: sent back to research with the note: Too long."]
    assert [(i.kind, i.stage, i.what) for i in e.interventions] == [
        ("send_back", "review", "research"),
        ("task", "research", "Check the source"),
    ]
    assert evidence(events, "run_2").decisions == ["at review: picked 2"]
    unknown = evidence(events, "run_9")
    assert (unknown.name, unknown.verdicts, unknown.interventions) == ("run_9", [], [])


def test_the_same_intervention_in_three_runs_recurs_and_in_two_does_not() -> None:
    sent = [decision("review", send_back_to="research", note="Shorter.")]
    noted = [decision("review", note="Fewer words!")]
    events = [
        run_event("run_1", sent),
        run_event("run_2", sent),
        run_event("run_3", [decision("review", send_back_to="research", note="Other note")]),
        run_event("run_4", noted),
        run_event("run_5", [decision("review", note="  fewer   words ")]),
        run_event("run_6", noted, workflow_id="wf_other"),
        run_event("run_7", sent, workflow_id="wf_other"),
    ]
    assert recurrences(events, "wf_1") == [
        Recurrence("send_back", "review", "research", ["run_1", "run_2", "run_3"])
    ]
    assert recurrences(events, "wf_other") == []
    events.append(run_event("run_8", noted))
    assert [r.kind for r in recurrences(events, "wf_1")] == ["note", "send_back"]


@given(st.integers(min_value=0, max_value=6))
def test_a_task_title_recurs_at_exactly_the_threshold(n: int) -> None:
    events = []
    for i in range(n):
        run_id = f"run_{i}"
        events.append(run_event(run_id, []))
        events.append(
            ev(
                "task.updated",
                {
                    "id": f"t{i}",
                    "run_id": run_id,
                    "stage_id": f"{run_id}_s0",
                    "title": "Check the source.",
                    "added_by": "operator",
                },
                run_id,
            )  # fmt: skip
        )
    found = recurrences(events, "wf_1")
    assert bool(found) == (n >= RECURRENCE)
    if found:
        assert found[0].what == "check the source" and len(found[0].runs) == n


def test_the_prompt_carries_evidence_skills_priors_and_the_yaml_only_when_something_recurs() -> (
    None
):
    e = evidence([run_event("run_1", [decision("review", note="Tighter.")])], "run_1")
    text = prompt(
        e, [], [("hooks", SKILL)], ["2026-10-20 run run_0: nothing (rejected)"], "name: x"
    )
    assert "at review: approved with the note: Tighter." in text
    assert "## hooks" in text and "Write the hook first." in text
    assert "run run_0: nothing (rejected)" in text and "name: x" not in text
    rec = [Recurrence("send_back", "review", "research", ["run_1", "run_2", "run_3"])]
    text = prompt(e, rec, [], [], "name: x")
    assert "(none yet)" in text and "name: x" in text and "send_back at review: research" in text


def test_parse_accepts_nothing_and_one_skill_change_with_evidence() -> None:
    assert parse_retro(None) == parse_retro({}) == parse_retro({"skill": None, "workflow": None})
    assert parse_retro({}).skill is None and parse_retro({}).workflow is None
    out = parse_retro(
        {
            "skill": {
                "action": "new",
                "name": "hooks",
                "content": SKILL,
                "summary": " Hook first ",
                "evidence": ["review: sent back", " "],
            },  # fmt: skip
            "workflow": {"summary": "Add a check", "yaml": "name: y\n", "evidence": ["x"]},
        }
    )
    assert out.skill is not None and out.workflow is not None
    assert (out.skill.action, out.skill.name, out.skill.summary) == ("new", "hooks", "Hook first")
    assert out.skill.evidence == ["review: sent back"] and out.workflow.yaml == "name: y\n"
    retired = parse_retro({"skill": {"action": "retired", "name": "hooks", "evidence": ["e"]}})
    assert retired.skill is not None and retired.skill.content == ""


@pytest.mark.parametrize(
    "bad",
    [
        "no",
        {"skill": "x"},
        {"skill": {"action": "edit", "name": "hooks", "content": SKILL, "evidence": ["e"]}},
        {"skill": {"action": "new", "name": "hooks", "content": SKILL, "evidence": []}},
        {"skill": {"action": "new", "name": "Hooks", "content": SKILL, "evidence": ["e"]}},
        {"skill": {"action": "new", "name": "hooks", "content": "just text", "evidence": ["e"]}},
        {"skill": {"action": "new", "name": "other", "content": SKILL, "evidence": ["e"]}},
        {"skill": {"action": "retired", "name": "", "evidence": ["e"]}},
        {"workflow": {"summary": "s", "yaml": "  ", "evidence": ["e"]}},
        {"workflow": {"summary": "s", "yaml": "name: y", "evidence": []}},
        {"workflow": []},
    ],
)
def test_a_malformed_answer_is_refused(bad: Any) -> None:
    with pytest.raises(ValueError):
        parse_retro(bad)


def test_check_skill_wants_the_spec_frontmatter() -> None:
    check_skill("hooks", SKILL)
    assert frontmatter("---\nname: 'a'\ndescription: \"b: c\"\n---\nbody") == {
        "name": "a",
        "description": "b: c",
    }
    assert frontmatter("no fences") == {}
    for name, content in [
        ("a" * 65, SKILL),
        ("hooks", "---\nname: hooks\n---\n"),
        ("hooks", "---\nname: hook\ndescription: d\n---\n"),
    ]:
        with pytest.raises(ValueError):
            check_skill(name, content)


def test_a_failed_run_shows_the_retro_its_failure_and_the_failed_stages_log_tail() -> None:
    stages = [
        {"id": "s0", "name": "research", "type": "swarm", "status": "failed", "error": "Boom"},
        {"id": "s1", "name": "retro", "type": "retro", "status": "running", "error": None},
    ]
    log = [
        ev(
            "agent.logged",
            {
                "stage_id": "s0",
                "agent": "worker-1",
                "kind": "tool",
                "tool": "web_search",
                "detail": "",
            },
            "run_1",
        ),  # fmt: skip
        *[
            ev(
                "agent.logged",
                {
                    "stage_id": "s0",
                    "agent": "worker-1",
                    "kind": "error",
                    "tool": None,
                    "detail": f"died {n}",
                },
                "run_1",
            )  # fmt: skip
            for n in range(LOG_TAIL)
        ],
        ev(
            "agent.logged",
            {
                "stage_id": "s1",
                "agent": "retro",
                "kind": "session",
                "tool": None,
                "detail": "Started.",
            },
            "run_1",
        ),  # fmt: skip
    ]
    events = [run_event("run_1", [], status="running", stages=stages), *log]
    e = evidence(events, "run_1")
    assert e.status == "failed"
    assert e.failure[0] == "research failed: Boom"
    assert e.failure[1:] == [f"worker-1 error: died {n}" for n in range(LOG_TAIL)]
    text = prompt(e, [], [], [], None)
    assert "which ended failed" in text and "# The failure" in text and "died 0" in text
    quiet = evidence([run_event("run_2", [])], "run_2")
    assert "# The failure" not in prompt(quiet, [], [], [], None)
