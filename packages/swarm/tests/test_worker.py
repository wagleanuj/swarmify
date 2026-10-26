"""One worker on a swarm stage, through the real Agent SDK client with a scripted CLI behind its
Transport seam. Hooks and in-process tools run for real; assertions read the live log, the
whiteboard and the files, as the API would."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from swarm_db import DiskFiles, DiskGraphs
from swarmify_swarm import OPERATOR, WORKER, SdkRuntime, SqliteStorage, StageError, Swarm, tool_id
from swarmify_swarm.fake import Call, Die, Say, Script

W, R, S = "world_1", "run_1", "stage_1"
STAGE = {"goal": "Find 5 ideas.", "done_when": "5 ideas ranked", "tools": []}
GRAPH = Call(tool_id("query_graph"), {"question": "What ideas did we reject?"})
CLAIM = Call(tool_id("claim_task"))


def _swarm(board: SqliteStorage, tmp_path: Path, script: Script, **kw: object) -> Swarm:
    return Swarm(board, SdkRuntime(transport=script.transport), DiskFiles(tmp_path), **kw)  # type: ignore[arg-type]


def _world(tmp_path: Path) -> Path:
    folder = tmp_path / W
    folder.mkdir(exist_ok=True)
    return folder


def _run(swarm: Swarm, tmp_path: Path, stage: dict[str, object] = STAGE) -> None:
    swarm.run_stage(W, R, S, "research", dict(stage), _world(tmp_path))


def _tools(board: SqliteStorage, kind: str = "tool") -> list[str]:
    return [e.tool or "" for e in board.list_log(S) if e.kind == kind]


def test_graph_query_then_claim_then_write_then_finish(
    board: SqliteStorage, tmp_path: Path
) -> None:
    task_id = board.add_task(W, R, S, "Pull top shorts", added_by=OPERATOR).id
    script = Script(
        [
            GRAPH,
            CLAIM,
            Call(tool_id("heartbeat"), {"task_id": task_id, "note": "Half way."}),
            Call(
                tool_id("write_note"), {"title": "shorts", "body": "Hooks win.", "task_id": task_id}
            ),
            Call(tool_id("write_artifact"), {"name": "ideas.md", "content": "1. Hooks"}),
            Call(tool_id("add_task"), {"title": "Rank the ideas"}),
            Call(tool_id("finish_task"), {"task_id": task_id, "note": "Five found."}),
            Say("Done."),
        ],
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
    added = [t for t in board.list_tasks(S) if t.title == "Rank the ideas"]
    assert added and added[0].added_by == WORKER
    stage_dir = tmp_path / W / "runs" / R / S
    assert (stage_dir / "notes" / "shorts.md").read_text() == f"{WORKER} {task_id}\nHooks win."
    assert (stage_dir / "artifacts" / "ideas.md").read_text() == "1. Hooks"
    assert script.sessions[0].cwd == stage_dir

    (agent,) = board.list_agents(R)
    assert agent.status == "done" and agent.cost_usd == 0.0123
    assert agent.session_id and agent.session_id.startswith("session-")
    kinds = [e.kind for e in board.list_log(S)]
    assert kinds[0] == "session" and kinds[-1] == "result"


def test_an_empty_board_is_seeded_with_the_stage_goal(board: SqliteStorage, tmp_path: Path) -> None:
    _run(_swarm(board, tmp_path, Script([GRAPH, CLAIM])), tmp_path)
    (task,) = board.list_tasks(S)
    assert (task.title, task.added_by, task.status) == ("Find 5 ideas.", "swarmify", "claimed")


def test_a_claim_before_any_graph_query_is_denied(board: SqliteStorage, tmp_path: Path) -> None:
    script = Script([CLAIM, GRAPH, CLAIM])
    _run(_swarm(board, tmp_path, script), tmp_path)
    denied = [e for e in board.list_log(S) if e.kind == "denied"]
    assert [e.tool for e in denied] == [tool_id("claim_task")]
    assert "Query the graph first" in denied[0].detail
    assert [(o.tool, o.is_error) for o in script.outcomes] == [
        (tool_id("claim_task"), True),
        (tool_id("query_graph"), False),
        (tool_id("claim_task"), False),
    ]
    assert script.outcomes[0].text == denied[0].detail


def test_tools_outside_the_stage_allowlist_are_denied(board: SqliteStorage, tmp_path: Path) -> None:
    world = _world(tmp_path)
    inside = world / "skills" / "verify" / "SKILL.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("---\nname: verify\ndescription: Check facts.\n---\n")
    script = Script(
        [
            Call("Bash", {"command": "rm -rf /"}),
            Call("WebSearch", {"query": "shorts"}),
            Call("mcp__fal__run", {"prompt": "a cat"}),
            Call("mcp__youtube__upload", {}),
            Call("Read", {"file_path": str(tmp_path / ".env")}),
            Call("Read", {"file_path": str(inside)}),
        ]
    )
    servers: list[list[str]] = []

    def connectors(world_id: str, names: list[str]) -> dict[str, dict[str, object]]:
        servers.append(names)
        return {n: {"type": "stdio", "command": n} for n in names}

    stage = {**STAGE, "tools": ["web_search", "fal"]}
    _run(_swarm(board, tmp_path, script, servers=connectors), tmp_path, stage)
    assert _tools(board, "denied") == ["Bash", "mcp__youtube__upload", "Read"]
    assert _tools(board) == ["WebSearch", "mcp__fal__run", "Read"]
    assert servers == [["fal"]]
    options = script.sessions[0]
    assert options.mcp_servers["fal"] == {"type": "stdio", "command": "fal"}  # type: ignore[index]
    assert "WebSearch" in options.allowed_tools and "mcp__fal__*" in options.allowed_tools
    assert options.permission_mode == "dontAsk"


def test_world_skills_load_as_a_plugin_without_host_settings(
    board: SqliteStorage, tmp_path: Path
) -> None:
    world = _world(tmp_path)
    for name in ("hooks", "verify"):
        (world / "skills" / name).mkdir(parents=True)
        (world / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    script = Script([GRAPH])
    _run(_swarm(board, tmp_path, script), tmp_path)

    options = script.sessions[0]
    assert options.setting_sources == [] and options.plugins == [
        {"type": "local", "path": str(world)}
    ]
    (agent,) = board.list_agents(R)
    assert agent.skills == [f"{W}:hooks", f"{W}:verify"]  # no host-skill leaked in
    started = board.list_log(S)[0]
    assert started.kind == "session" and f"{W}:hooks, {W}:verify" in started.detail


def test_a_plugin_that_fails_to_load_is_logged_as_an_error(
    board: SqliteStorage, tmp_path: Path
) -> None:
    swarm = _swarm(board, tmp_path, Script([GRAPH]))
    swarm.run_stage(W, R, S, "research", dict(STAGE), tmp_path / "gone")
    first = board.list_log(S)[0]
    assert first.kind == "error"
    assert "does not exist" in first.detail


def test_a_replacement_agent_resumes_the_stored_session(
    board: SqliteStorage, tmp_path: Path
) -> None:
    script = Script([GRAPH, Die()], [CLAIM, Say("Picked up where I left off.")])
    _run(_swarm(board, tmp_path, script), tmp_path)

    first, second = script.sessions
    assert first.resume is None
    (agent,) = board.list_agents(R)
    assert second.resume == agent.session_id and agent.status == "done"
    details = [e.detail for e in board.list_log(S)]
    assert any("The session died" in d for d in details)
    assert any(f"A replacement resumes session {agent.session_id}" in d for d in details)
    # The graph query of the first session still counts for the replacement's claim.
    assert _tools(board) == [tool_id("query_graph"), tool_id("claim_task")]


def test_the_stage_fails_when_the_replacement_dies_too(
    board: SqliteStorage, tmp_path: Path
) -> None:
    script = Script([Die()])
    with pytest.raises(StageError, match="failed 2 sessions"):
        _run(_swarm(board, tmp_path, script), tmp_path)
    assert len(script.sessions) == 2
    (agent,) = board.list_agents(R)
    assert agent.status == "failed" and script.sessions[1].resume == agent.session_id


def test_a_finished_worker_does_not_run_again(board: SqliteStorage, tmp_path: Path) -> None:
    script = Script([GRAPH])
    swarm = _swarm(board, tmp_path, script)
    _run(swarm, tmp_path)
    _run(swarm, tmp_path)
    assert len(script.sessions) == 1


def test_tool_refusals_reach_the_agent_as_errors(board: SqliteStorage, tmp_path: Path) -> None:
    script = Script(
        [
            GRAPH,
            Call(tool_id("finish_task"), {"task_id": "task_nope"}),
            Call(tool_id("write_artifact"), {"name": "../escape.md", "content": "x"}),
            CLAIM,
            CLAIM,
        ]
    )
    _run(_swarm(board, tmp_path, script), tmp_path)
    errors = [o.text for o in script.outcomes if o.is_error]
    assert errors == [
        "No such task on this whiteboard",
        "not a plain file name",
        "No task is ready to claim. Add one with add_task, or stop.",
    ]
    listed = json.loads(script.outcomes[-2].text)
    assert listed["owner"] == WORKER


def test_a_task_of_another_stage_cannot_be_claimed_or_finished(
    board: SqliteStorage, tmp_path: Path
) -> None:
    foreign = board.add_task(W, "run_OTHER", "stage_OTHER", "Theirs", added_by=OPERATOR).id
    script = Script(
        [
            GRAPH,
            Call(tool_id("claim_task"), {"task_id": foreign}),
            Call(tool_id("heartbeat"), {"task_id": foreign}),
            Call(tool_id("finish_task"), {"task_id": foreign, "note": "hijacked"}),
        ]
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

    script = Script([GRAPH, Die()], [Say("ok")])
    _run(_swarm(board, tmp_path, script), tmp_path)
    options = replace(script.sessions[1], cli_path="/bin/claude")  # never started
    cmd = SubprocessCLITransport(prompt="", options=options)._build_command()
    assert "--setting-sources=" in cmd
    assert cmd[cmd.index("--plugin-dir") + 1] == str(tmp_path / W)
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert f"--resume={script.sessions[1].resume}" in cmd


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
        ]
    )
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
        (False, "[world market]"),
        (False, "[world market]"),
    ]
    for o in script.outcomes[3:]:
        assert o.is_error and f"Readable: {W}, market." in o.text
