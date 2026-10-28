"""One worker on a swarm stage, through the real Agent SDK client with a scripted CLI behind its
Transport seam. Hooks and in-process tools run for real; assertions read the live log, the
whiteboard and the files, as the API would. A judge session that passes ends every stage."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from swarm_db import DiskFiles, DiskGraphs
from swarmify_swarm import (
    OPERATOR,
    WORKER,
    Agent,
    SdkRuntime,
    SqliteStorage,
    StageError,
    Stopped,
    Swarm,
    tool_id,
)
from swarmify_swarm.fake import LAST, Answer, Call, Die, Say, Script, Step
from swarmify_swarm.projection import PLAN_SCHEMA
from swarmify_swarm.sdk import HARNESS

W, R, S = "world_1", "run_1", "stage_1"
STAGE = {"goal": "Find 5 ideas.", "done_when": "5 ideas ranked", "tools": []}
GRAPH = Call(tool_id("query_graph"), {"question": "What ideas did we reject?"})
CLAIM = Call(tool_id("claim_task"))
FINISH = Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."})
PASS: list[Step] = [Answer({"passed": True, "reasons": ["Five ideas, ranked."]})]


def _swarm(board: SqliteStorage, tmp_path: Path, script: Script, **kw: object) -> Swarm:
    return Swarm(board, SdkRuntime(transport=script.transport), DiskFiles(tmp_path), **kw)  # type: ignore[arg-type]


def _world(tmp_path: Path) -> Path:
    folder = tmp_path / W
    folder.mkdir(exist_ok=True)
    return folder


def _run(swarm: Swarm, tmp_path: Path, stage: dict[str, object] = STAGE) -> None:
    swarm.run_stage(W, R, S, "research", dict(stage), _world(tmp_path))


def _seed(board: SqliteStorage, title: str = "Pull top shorts") -> str:
    """An operator task, so the planner has nothing to seed and the worker starts right away."""
    return board.add_task(W, R, S, title, added_by=OPERATOR).id


def _tools(board: SqliteStorage, kind: str = "tool") -> list[str]:
    return [e.tool or "" for e in board.list_log(S) if e.kind == kind]


def _worker(board: SqliteStorage) -> Agent:
    (agent,) = [a for a in board.list_agents(R) if a.name == WORKER]
    return agent


def test_graph_query_then_claim_then_write_then_finish(
    board: SqliteStorage, tmp_path: Path
) -> None:
    task_id = _seed(board)
    script = Script(
        [
            GRAPH,
            CLAIM,
            Call(tool_id("heartbeat"), {"task_id": task_id, "note": "Half way."}),
            Call(
                tool_id("write_note"), {"title": "shorts", "body": "Hooks win.", "task_id": task_id}
            ),
            Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"}),
            Call(tool_id("finish_task"), {"task_id": task_id, "note": "Five found."}),
            Say("Done."),
        ],
        PASS,
        cost=0.0123,
    )
    graphs = DiskGraphs(tmp_path)
    _run(_swarm(board, tmp_path, script, graphs=graphs), tmp_path)

    order = _tools(board)
    assert order.index(tool_id("query_graph")) < order.index(tool_id("claim_task"))
    assert order.index(tool_id("claim_task")) < order.index(tool_id("write_note"))
    assert not any(o.is_error for o in script.outcomes), script.outcomes
    assert graphs.calls == [(W, "query_graph", {"question": "What ideas did we reject?"})]
    assert script.outcomes[0].text == f"[world {W}]\nHooks work."

    task = board.get_task(task_id)
    assert task and task.status == "done" and task.owner == WORKER
    assert [n.body for n in task.notes] == ["Half way.", "Five found."]
    stage_dir = tmp_path / W / "runs" / R / S
    assert (stage_dir / "notes" / "shorts.md").read_text() == f"{WORKER} {task_id}\nHooks win."
    assert (stage_dir / "artifacts" / "ideas.md").read_text() == "1. Hooks"
    assert script.sessions[0].cwd == stage_dir

    agent = _worker(board)
    assert agent.status == "done" and agent.cost_usd == 0.0123
    assert agent.session_id and agent.session_id.startswith("session-")
    kinds = [e.kind for e in board.list_log(S) if e.agent == WORKER]
    assert kinds[0] == "session" and kinds[-1] == "result"


def test_the_planner_seeds_an_empty_board_from_its_json_answer(
    board: SqliteStorage, tmp_path: Path
) -> None:
    plan = {
        "tasks": [
            {"title": "Pull top shorts", "body": "This week only."},
            {"title": "Rank them", "after": [0, 5]},
        ]
    }
    script = Script([GRAPH, Answer(plan)], [GRAPH, CLAIM, FINISH, CLAIM, FINISH], PASS)
    _run(_swarm(board, tmp_path, script), tmp_path, {**STAGE, "produces": ["ideas.md"]})

    first, second = board.list_tasks(S)
    assert (first.title, first.body, first.added_by) == (
        "Pull top shorts",
        "This week only.",
        "planner",
    )
    assert (second.title, second.depends_on) == ("Rank them", [first.id])
    assert first.status == second.status == "done"

    planner = script.sessions[0]
    assert planner.output_format == {"type": "json_schema", "schema": PLAN_SCHEMA}
    assert tool_id("claim_task") not in planner.allowed_tools
    assert tool_id("query_graph") in planner.allowed_tools
    assert "Goal: Find 5 ideas." in script.prompts[0] and "ideas.md" in script.prompts[0]
    roles = {a.name: (a.role, a.status) for a in board.list_agents(R)}
    assert roles == {
        "planner": ("planner", "done"),
        WORKER: ("worker", "done"),
        "judge": ("judge", "done"),
    }
    assert [e.tool for e in board.list_log(S) if e.agent == "planner" and e.kind == "tool"] == [
        tool_id("query_graph")
    ]


def test_a_planner_answer_without_tasks_is_retried_then_fails_the_stage(
    board: SqliteStorage, tmp_path: Path
) -> None:
    script = Script([Answer({"tasks": []})], [Say("no json")])
    with pytest.raises(StageError, match="planner failed 2 sessions"):
        _run(_swarm(board, tmp_path, script), tmp_path)
    assert board.list_tasks(S) == []
    errors = [e.detail for e in board.list_log(S) if e.kind == "error"]
    assert errors == [
        "The answer was not usable: the planner returned no tasks",
        "The answer was not usable: expected a JSON object, got NoneType",
    ]


def test_a_fail_adds_gap_tasks_and_the_stage_loops_until_a_pass(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    fail = Answer({"passed": False, "reasons": ["Only 3 ideas."], "gaps": ["Find 2 more ideas"]})
    script = Script(
        [GRAPH, CLAIM, FINISH],
        [fail],
        [CLAIM, FINISH],
        PASS,
    )
    _run(_swarm(board, tmp_path, script), tmp_path)

    v1, v2 = board.list_verdicts(S)
    assert (v1.loop, v1.passed, v1.reasons, v1.gaps) == (
        1,
        False,
        ["Only 3 ideas."],
        ["Find 2 more ideas"],
    )
    assert (v2.loop, v2.passed, v2.gaps) == (2, True, [])
    gap = board.list_tasks(S)[-1]
    assert (gap.title, gap.added_by, gap.status) == ("Find 2 more ideas", "judge", "done")
    # The worker's second round continues its session; each verdict is a fresh judge session.
    assert script.sessions[2].resume == _worker(board).session_id
    assert script.sessions[1].resume is None and script.sessions[3].resume is None
    assert (
        "loop 2 of at most 4" in script.prompts[3]
        and "Find 2 more ideas (done)" in script.prompts[3]
        and "verdict-1.md" in script.prompts[3]
    )

    verdicts = sorted((tmp_path / W / "runs" / R / S / "artifacts").glob("verdict-*.md"))
    assert [p.name for p in verdicts] == ["verdict-1.md", "verdict-2.md"]
    assert (
        "Only 3 ideas." in verdicts[0].read_text()
        and "Find 2 more ideas" in verdicts[0].read_text()
    )
    assert v1.path == f"runs/{R}/{S}/artifacts/verdict-1.md"


def test_the_fourth_failed_verdict_fails_the_stage(board: SqliteStorage, tmp_path: Path) -> None:
    _seed(board)
    fail = Answer({"passed": False, "reasons": ["Still thin."]})
    script = Script([GRAPH, CLAIM, FINISH], [fail], cycle=True)
    with pytest.raises(StageError, match="failed the stage 4 times.*Still thin"):
        _run(_swarm(board, tmp_path, script), tmp_path)
    verdicts = board.list_verdicts(S)
    assert [(v.loop, v.passed) for v in verdicts] == [
        (1, False),
        (2, False),
        (3, False),
        (4, False),
    ]
    # A fail without gaps still leaves work: one gap task per reason. The final verdict fails
    # the stage, so it adds none.
    assert [t.title for t in board.list_tasks(S)].count("Close the gap: Still thin.") == 3
    assert verdicts[-1].gaps == ["Close the gap: Still thin."]
    assert len(script.sessions) == 8  # four rounds of work, four verdicts, no fifth

    with pytest.raises(StageError, match="4 times"):  # a retried stage fails the same way
        _run(_swarm(board, tmp_path, script), tmp_path)
    assert len(script.sessions) == 8


def test_a_pass_projects_the_declared_output_type_with_source_references(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    note = Call(tool_id("write_note"), {"title": "trends", "body": "Hooks win.\n\nChases trend."})
    note_path = f"runs/{R}/{S}/notes/trends.md"
    verdict = {
        "passed": True,
        "reasons": ["Two ideas, ranked."],
        "items": [
            {
                "title": "The chase",
                "body": "A chase in 30 seconds.",
                "rank": 1,
                "fields": {"hook": "Run!", "why": "Chases trend."},
                "sources": [
                    {"note": note_path, "paragraph": 2},
                    {"note": note_path, "paragraph": 9},
                    {"note": "runs/elsewhere.md", "paragraph": 1},
                ],
            },
            {"title": "Hooks", "body": "First second.", "rank": 2, "sources": []},
        ],
    }
    script = Script([GRAPH, CLAIM, note, FINISH], [Answer(verdict)])
    files = DiskFiles(tmp_path)
    swarm = Swarm(board, SdkRuntime(transport=script.transport), files)
    _run(swarm, tmp_path, {**STAGE, "output_type": "list[idea]"})

    assert f"## {note_path}\n[1] {WORKER} None\nHooks win.\n[2] Chases trend." in script.prompts[1]
    assert "items of type idea" in script.prompts[1]
    typed = {name: (type_, fields) for name, type_, fields, _ in files.typed}
    assert typed["idea-the-chase.md"] == (
        "idea",
        {
            "hook": "Run!",
            "why": "Chases trend.",
            "title": "The chase",
            "rank": 1,
            "sources": [f"{note_path}#p2"],
        },
    )
    assert typed["idea-hooks.md"][1]["sources"] == []
    assert typed["verdict-1.md"][0] == "verdict" and typed["verdict-1.md"][1]["passed"] is True
    chase = (tmp_path / W / "runs" / R / S / "artifacts" / "idea-the-chase.md").read_text()
    assert f'{note_path} paragraph 2: "Chases trend."' in chase

    # A retried stage projects the stored verdict again without another judge session.
    _run(swarm, tmp_path, {**STAGE, "output_type": "list[idea]"})
    assert len(script.sessions) == 2
    assert [n for n, *_ in files.typed].count("idea-the-chase.md") == 2


def test_a_worker_that_stops_with_tasks_left_fails_the_stage_unjudged(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    script = Script([GRAPH, CLAIM, FINISH, Call(tool_id("add_task"), {"title": "Rank the ideas"})])
    with pytest.raises(StageError, match=r"stopped with 1 task\(s\) not done: Rank the ideas"):
        _run(_swarm(board, tmp_path, script), tmp_path)
    added = board.list_tasks(S)[-1]
    assert added.added_by == WORKER and added.status == "open"
    assert [a.name for a in board.list_agents(R)] == [WORKER]


def test_an_unknown_output_type_fails_the_stage(board: SqliteStorage, tmp_path: Path) -> None:
    with pytest.raises(StageError, match="not an output type"):
        _run(_swarm(board, tmp_path, Script()), tmp_path, {**STAGE, "output_type": "list[Idea"})


def test_a_claim_before_any_graph_query_is_denied(board: SqliteStorage, tmp_path: Path) -> None:
    _seed(board)
    script = Script([CLAIM, GRAPH, CLAIM, FINISH], PASS)
    _run(_swarm(board, tmp_path, script), tmp_path)
    denied = [e for e in board.list_log(S) if e.kind == "denied"]
    assert [e.tool for e in denied] == [tool_id("claim_task")]
    assert "Query the graph first" in denied[0].detail
    assert [(o.tool, o.is_error) for o in script.outcomes] == [
        (tool_id("claim_task"), True),
        (tool_id("query_graph"), False),
        (tool_id("claim_task"), False),
        (tool_id("finish_task"), False),
    ]
    assert script.outcomes[0].text == denied[0].detail


def test_tools_outside_the_stage_allowlist_are_denied(board: SqliteStorage, tmp_path: Path) -> None:
    _seed(board)
    world = _world(tmp_path)
    inside = world / "skills" / "verify" / "SKILL.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("---\nname: verify\ndescription: Check facts.\n---\n")
    script = Script(
        [
            Call("Bash", {"command": "rm -rf /"}),
            Call("ToolSearch", {"query": "select:mcp__swarmify__query_graph"}),
            Call("WebSearch", {"query": "shorts"}),
            Call("mcp__fal__run", {"prompt": "a cat"}),
            Call("mcp__youtube__upload", {}),
            Call("Read", {"file_path": str(tmp_path / ".env")}),
            Call("Read", {"file_path": str(inside)}),
            GRAPH,
            CLAIM,
            FINISH,
        ],
        PASS,
    )
    servers: list[list[str]] = []

    def connectors(world_id: str, names: list[str]) -> dict[str, dict[str, object]]:
        servers.append(names)
        return {n: {"type": "stdio", "command": n} for n in names}

    stage = {**STAGE, "tools": ["web_search", "fal"]}
    _run(_swarm(board, tmp_path, script, servers=connectors), tmp_path, stage)
    assert _tools(board, "denied") == ["Bash", "mcp__youtube__upload", "Read"]
    assert _tools(board)[:3] == ["WebSearch", "mcp__fal__run", "Read"]
    assert servers == [["fal"]]
    options = script.sessions[0]
    assert options.mcp_servers["fal"] == {"type": "stdio", "command": "fal"}  # type: ignore[index]
    assert "WebSearch" in options.allowed_tools and "mcp__fal__*" in options.allowed_tools
    assert options.permission_mode == "dontAsk"
    judge = script.sessions[1]
    assert "WebSearch" not in judge.allowed_tools and "fal" not in judge.mcp_servers  # type: ignore[operator]
    # CLI mechanics pass whatever the allowlist: denying them left planners with no answer.
    assert all(t in judge.allowed_tools for t in HARNESS)


def test_world_skills_load_as_a_plugin_without_host_settings(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    world = _world(tmp_path)
    for name in ("hooks", "verify"):
        (world / "skills" / name).mkdir(parents=True)
        (world / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    script = Script([GRAPH, CLAIM, FINISH], PASS)
    _run(_swarm(board, tmp_path, script), tmp_path)

    options = script.sessions[0]
    assert options.setting_sources == [] and options.plugins == [
        {"type": "local", "path": str(world)}
    ]
    assert _worker(board).skills == [f"{W}:hooks", f"{W}:verify"]  # no host-skill leaked in
    started = board.list_log(S)[0]
    assert started.kind == "session" and f"{W}:hooks, {W}:verify" in started.detail


def test_a_plugin_that_fails_to_load_is_logged_as_an_error(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    swarm = _swarm(board, tmp_path, Script([GRAPH, CLAIM, FINISH], PASS))
    swarm.run_stage(W, R, S, "research", dict(STAGE), tmp_path / "gone")
    first = board.list_log(S)[0]
    assert first.kind == "error"
    assert "does not exist" in first.detail


def test_a_replacement_agent_resumes_the_stored_session(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    script = Script([GRAPH, Die()], [CLAIM, FINISH, Say("Picked up where I left off.")], PASS)
    _run(_swarm(board, tmp_path, script), tmp_path)

    first, second, _ = script.sessions
    assert first.resume is None
    agent = _worker(board)
    assert second.resume == agent.session_id and agent.status == "done"
    details = [e.detail for e in board.list_log(S)]
    assert any("The session died" in d for d in details)
    assert any(f"A replacement resumes session {agent.session_id}" in d for d in details)
    # The graph query of the first session still counts for the replacement's claim.
    assert _tools(board) == [tool_id(t) for t in ("query_graph", "claim_task", "finish_task")]


def test_the_stage_fails_when_the_replacement_dies_too(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    script = Script([Die()])
    with pytest.raises(StageError, match="worker-1 failed 2 sessions"):
        _run(_swarm(board, tmp_path, script), tmp_path)
    assert len(script.sessions) == 2
    agent = _worker(board)
    assert agent.status == "failed" and script.sessions[1].resume == agent.session_id


def test_a_retried_stage_does_not_plan_work_or_judge_again(
    board: SqliteStorage, tmp_path: Path
) -> None:
    script = Script([Answer({"tasks": [{"title": "One"}]})], [GRAPH, CLAIM, FINISH], PASS)
    swarm = _swarm(board, tmp_path, script)
    _run(swarm, tmp_path)
    _run(swarm, tmp_path)
    assert len(script.sessions) == 3
    assert len(board.list_tasks(S)) == 1 and len(board.list_verdicts(S)) == 1


def test_tool_refusals_reach_the_agent_as_errors(board: SqliteStorage, tmp_path: Path) -> None:
    _seed(board)
    script = Script(
        [
            GRAPH,
            Call(tool_id("finish_task"), {"task_id": "task_nope"}),
            Call(tool_id("write_artifact"), {"name": "../escape.md", "content": "x"}),
            CLAIM,
            CLAIM,
            FINISH,
        ],
        PASS,
    )
    _run(_swarm(board, tmp_path, script), tmp_path)
    errors = [o.text for o in script.outcomes if o.is_error]
    assert errors == [
        "No such task on this whiteboard",
        "not a plain file name",
        "No task is ready to claim. Add one with add_task, or stop.",
    ]
    listed = json.loads(script.outcomes[3].text)
    assert listed["owner"] == WORKER


def test_a_task_of_another_stage_cannot_be_claimed_or_finished(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    foreign = board.add_task(W, "run_OTHER", "stage_OTHER", "Theirs", added_by=OPERATOR).id
    script = Script(
        [
            GRAPH,
            Call(tool_id("claim_task"), {"task_id": foreign}),
            Call(tool_id("heartbeat"), {"task_id": foreign}),
            Call(tool_id("finish_task"), {"task_id": foreign, "note": "hijacked"}),
            CLAIM,
            FINISH,
        ],
        PASS,
    )
    _run(_swarm(board, tmp_path, script), tmp_path)
    assert [o.text for o in script.outcomes if o.is_error] == [
        "No such task on this whiteboard"
    ] * 3
    task = board.get_task(foreign)
    assert task and (task.status, task.owner, task.notes) == ("open", None, [])


def test_the_sdk_builds_the_cli_flags_the_contract_relies_on(
    board: SqliteStorage, tmp_path: Path
) -> None:
    """The real subprocess transport turns our options into these CLI flags."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    _seed(board)
    script = Script([GRAPH, Die()], [CLAIM, FINISH], PASS)
    _run(_swarm(board, tmp_path, script), tmp_path)
    options = replace(script.sessions[1], cli_path="/bin/claude")  # never started
    cmd = SubprocessCLITransport(prompt="", options=options)._build_command()
    assert "--setting-sources=" in cmd
    assert cmd[cmd.index("--plugin-dir") + 1] == str(tmp_path / W)
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert f"--resume={script.sessions[1].resume}" in cmd
    assert "--json-schema" not in cmd
    judge = replace(script.sessions[2], cli_path="/bin/claude")
    cmd = SubprocessCLITransport(prompt="", options=judge)._build_command()
    assert '"passed"' in cmd[cmd.index("--json-schema") + 1]


def test_graph_tools_read_only_the_worlds_the_stage_allows(
    board: SqliteStorage, tmp_path: Path
) -> None:
    market = tmp_path / "market"
    market.mkdir()
    script = Script(
        [
            GRAPH,
            Call(tool_id("god_nodes"), {"world": "market", "top_n": 3}),
            Call(tool_id("get_node"), {"label": "Hooks", "project_path": str(market)}),
            Call(tool_id("graph_stats"), {"world": "secret"}),
            Call(tool_id("query_graph"), {"question": "q", "project_path": str(tmp_path / "x")}),
            Call(tool_id("shortest_path"), {"source": "a", "target": "b", "world": "video"}),
            CLAIM,
            FINISH,
        ],
        PASS,
    )
    _seed(board)
    graphs = DiskGraphs(tmp_path)
    stage = {**STAGE, "graphs": ["market", "video"]}  # video names no world, so it is dropped
    _run(_swarm(board, tmp_path, script, graphs=graphs), tmp_path, stage)

    assert graphs.calls == [
        (W, "query_graph", {"question": "What ideas did we reject?"}),
        ("market", "god_nodes", {"top_n": 3}),
        ("market", "get_node", {"label": "Hooks"}),
    ]
    outcomes = [(o.is_error, o.text.splitlines()[0]) for o in script.outcomes]
    assert outcomes[:3] == [
        (False, f"[world {W}]"),
        (False, "[world Market]"),
        (False, "[world Market]"),
    ]
    for o in script.outcomes[3:6]:
        assert o.is_error and f"Readable: {W}, market." in o.text


def test_pinned_artifacts_are_required_reading_for_the_planner_and_the_worker(
    board: SqliteStorage, tmp_path: Path
) -> None:
    plan = Answer({"tasks": [{"title": "Pull top shorts"}]})
    script = Script([GRAPH, plan], [GRAPH, CLAIM, FINISH], PASS)
    files = DiskFiles(tmp_path)
    files.pins = ["runs/run_0/stage_0/artifacts/brief.md"]
    _run(Swarm(board, SdkRuntime(transport=script.transport), files), tmp_path)  # type: ignore[arg-type]

    pinned = str(tmp_path / W / "runs/run_0/stage_0/artifacts/brief.md")
    planner, worker, judge = script.prompts
    assert "Pinned references, required reading" in planner and pinned in planner
    assert pinned in str(script.sessions[1].system_prompt) and pinned not in worker
    assert "Pinned" not in judge, "the judge judges the stage's own notes"


DOLLAR = {"input_tokens": 250_000}
"""One model step's usage that the adapter prices at $1.00."""


def test_every_tool_call_adds_its_estimate_and_the_result_trues_it_up(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    script = Script([GRAPH, CLAIM, FINISH, Say("Done.")], PASS, cost=3.5, usage=DOLLAR)
    seen: list[float] = []

    def budget(run_id: str) -> str | None:
        seen.append(sum(a.cost_usd for a in board.list_agents(run_id)))
        return None

    _run(_swarm(board, tmp_path, script, budget=budget), tmp_path)
    # Worker: before the session, after each tool call's step, after the result's remainder.
    # Judge: before its session and after its result (an answer calls no tool).
    assert seen == [0.0, 1.0, 2.0, 3.0, 3.5, 3.5, 7.0]
    assert _worker(board).cost_usd == 3.5


def test_a_spent_budget_stops_the_session_and_a_rerun_resumes_it(
    board: SqliteStorage, tmp_path: Path
) -> None:
    _seed(board)
    limit = [2.0]

    def budget(run_id: str) -> str | None:
        spent = sum(a.cost_usd for a in board.list_agents(run_id))
        return "The run's budget of $2.00 is spent." if spent >= limit[0] else None

    note = Call(tool_id("write_note"), {"title": "late", "body": "Too late."})
    script = Script(
        [GRAPH, CLAIM, note, FINISH, Say("Done.")],
        [CLAIM, FINISH, Say("Resumed.")],
        PASS,
        cost=0.5,
        usage=DOLLAR,
    )
    swarm = _swarm(board, tmp_path, script, budget=budget)
    with pytest.raises(Stopped, match="budget of \\$2.00 is spent"):
        _run(swarm, tmp_path)
    agent = _worker(board)
    assert agent.status == "working" and agent.cost_usd == 2.0
    assert _tools(board) == [GRAPH.tool]  # the session stopped before the claim ran
    assert board.list_tasks(S)[0].status == "open"
    assert "Stopped: The run's budget" in board.list_log(S)[-1].detail
    with pytest.raises(Stopped):  # still spent: no session starts at all
        _run(swarm, tmp_path)
    assert len(script.sessions) == 1
    assert board.list_log(S)[-1].detail.startswith("Not started: The run's budget")

    limit[0] = 10.0
    _run(swarm, tmp_path)
    assert script.sessions[1].resume == agent.session_id
    agent = _worker(board)
    assert agent.status == "done" and agent.cost_usd == 2.5
    assert len(board.list_verdicts(S)) == 1


def test_a_baseline_stage_is_one_worker_with_no_graph(board: SqliteStorage, tmp_path: Path) -> None:
    script = Script([CLAIM, GRAPH, FINISH, Say("Done.")])
    swarm = _swarm(board, tmp_path, script, graphs=DiskGraphs(tmp_path))
    swarm.run_stage(W, R, S, "research", dict(STAGE), _world(tmp_path), baseline=True)
    claim, graph, finish = script.outcomes
    assert not claim.is_error and not finish.is_error  # no graph query is asked for first
    assert graph.is_error and _tools(board, "denied") == [tool_id("query_graph")]
    (task,) = board.list_tasks(S)
    assert (task.title, task.added_by, task.status) == (STAGE["goal"], WORKER, "done")
    (options,) = script.sessions  # no planner, no judge
    assert not any("graph" in t for t in options.allowed_tools)
    assert "query_graph" not in str(options.system_prompt)
    assert [a.name for a in board.list_agents(R)] == [WORKER]
    assert board.list_verdicts(S) == []


def test_ask_runs_one_read_only_session_with_the_world_skills_and_the_run_budget(
    board: SqliteStorage, tmp_path: Path
) -> None:
    """Another context's question (the retro) goes through the same seam as the judge: logged
    under its name, costed, the world's skills loaded, a malformed answer retried once."""
    world = _world(tmp_path)
    skill = world / "skills" / "hooks" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hooks\ndescription: Hook first.\n---\n")
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    script = Script(
        [Call("Bash", {"command": "ls"}), Answer({"n": "not a number"})],
        [Call(tool_id("query_graph"), {"question": "What recurs?"}), Answer({"n": 2})],
        cost=0.05,
    )
    swarm = _swarm(board, tmp_path, script)

    def parse(output: object) -> int:
        n = output["n"] if isinstance(output, dict) else None
        if not isinstance(n, int):
            raise ValueError("n must be a number")
        return n

    answer = swarm.ask(
        W, R, "stage_retro", "retro", world, "retro", "You are the retro.", "Go.", schema, parse
    )
    assert answer == 2
    first, second = script.sessions
    assert first.system_prompt == "You are the retro." and first.output_format == {
        "type": "json_schema",
        "schema": schema,
    }
    assert first.plugins == [{"type": "local", "path": str(world)}] and first.setting_sources == []
    (agent,) = board.list_agents(R)
    assert second.resume == agent.session_id, "the replacement resumes the unusable session"
    assert (agent.name, agent.role, agent.status, agent.cost_usd) == ("retro", "judge", "done", 0.1)
    assert agent.skills == [f"{W}:hooks"] and agent.stage_id == "stage_retro"
    log = board.list_log("stage_retro")
    assert [e.kind for e in log if e.tool == "Bash"] == ["denied"]
    assert any(e.kind == "error" and "n must be a number" in e.detail for e in log)
    assert any(e.kind == "tool" and e.tool == tool_id("query_graph") for e in log)

    dead = _swarm(board, tmp_path, Script([Die()]))
    with pytest.raises(StageError, match="retro failed 2 sessions"):
        dead.ask(W, R, "stage_r2", "retro", world, "retro", "s", "p", schema, parse)
