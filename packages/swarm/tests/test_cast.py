"""The Cast at the swarm's seam: standing prompts reach the sessions, an agent's tools narrow
the stage's, and as many workers start as there are ready tasks, up to the cast's size."""

from pathlib import Path

from swarm_db import DiskFiles
from swarmify_swarm import OPERATOR, CastEntry, SdkRuntime, SqliteStorage, Swarm, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script

W, R, S = "world_1", "run_1", "stage_1"
GRAPH = Call(tool_id("query_graph"), {"question": "What do we know?"})
PASS = [Answer({"passed": True, "reasons": ["Done."]})]
CAST = [
    CastEntry("planner", "planner", "Keep plans to three tasks."),
    CastEntry("worker-1", "worker", "Write hooks first.", ["graph"]),
    CastEntry("worker-2", "worker"),
    CastEntry("judge", "judge", "Be strict about sources."),
]


def _swarm(board: SqliteStorage, tmp_path: Path, script: Script) -> Swarm:
    return Swarm(board, SdkRuntime(transport=script.transport), DiskFiles(tmp_path))  # type: ignore[arg-type]


def _run(board: SqliteStorage, tmp_path: Path, script: Script) -> None:
    (tmp_path / W).mkdir(exist_ok=True)
    stage = {"goal": "Find 5 ideas.", "done_when": "5 ideas ranked", "tools": ["web_search"]}
    _swarm(board, tmp_path, script).run_stage(W, R, S, "research", stage, tmp_path / W, cast=CAST)


def _claim_and_finish(task_id: str) -> list:
    return [
        GRAPH,
        Call(tool_id("claim_task"), {"task_id": task_id}),
        Call(tool_id("finish_task"), {"task_id": task_id, "note": "Done."}),
        Say("Done."),
    ]


def test_standing_prompts_reach_every_role_and_an_agents_tools_narrow_the_stages(
    board: SqliteStorage, tmp_path: Path
) -> None:
    plan = Answer({"tasks": [{"title": "Pull top shorts"}]})
    work = [GRAPH, Call(tool_id("claim_task")), Call(tool_id("finish_task"), {"task_id": LAST})]
    script = Script([plan], work, PASS)
    _run(board, tmp_path, script)

    planner, worker, judge = script.sessions
    assert "Your standing instructions: Keep plans to three tasks." in str(planner.system_prompt)
    assert "You are worker-1, a worker in a swarm." in str(worker.system_prompt)
    assert "Your standing instructions: Write hooks first." in str(worker.system_prompt)
    assert "Your standing instructions: Be strict about sources." in str(judge.system_prompt)
    # The stage allows web_search, worker-1's cast entry does not: WebSearch stays out.
    assert (
        "WebSearch" not in worker.allowed_tools and tool_id("query_graph") in worker.allowed_tools
    )
    assert {a.name for a in board.list_agents(R)} == {"planner", "worker-1", "judge"}


def test_two_ready_tasks_start_two_workers_who_share_the_whiteboard(
    board: SqliteStorage, tmp_path: Path
) -> None:
    first = board.add_task(W, R, S, "Pull top shorts", added_by=OPERATOR).id
    second = board.add_task(W, R, S, "Rank them", added_by=OPERATOR).id
    # Whichever worker gets which script, each claims its own task.
    script = Script(_claim_and_finish(first), _claim_and_finish(second), PASS)
    _run(board, tmp_path, script)

    agents = {a.name: a for a in board.list_agents(R)}
    assert {n for n, a in agents.items() if a.role == "worker"} == {"worker-1", "worker-2"}
    assert all(a.status == "done" for a in agents.values())
    tasks = {t.id: t for t in board.list_tasks(S)}
    assert {tasks[first].owner, tasks[second].owner} == {"worker-1", "worker-2"}
    assert len(script.sessions) == 3  # two workers, then the judge; nothing to plan
    assert not any(o.is_error for o in script.outcomes), script.outcomes
