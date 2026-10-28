"""Which model a session runs on, what the agent records of it, and what a step costs on it."""

from pathlib import Path

import pytest

from swarm_db import DiskFiles
from swarmify_swarm import OPERATOR, CastEntry, SdkRuntime, SqliteStorage, Swarm, tool_id
from swarmify_swarm.fake import LAST, Answer, Call, Say, Script
from swarmify_swarm.sdk import DEFAULT_MODEL, PRICES, step_cost

W, R, S = "world_1", "run_1", "stage_1"
MTOK = {"input_tokens": 1_000_000}


def test_each_model_has_its_own_price_and_an_unknown_one_costs_the_most() -> None:
    opus, sonnet, haiku = (
        step_cost(MTOK, m) for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5")
    )
    assert (opus, sonnet, haiku) == (4.0, 2.0, 1.0)
    assert step_cost({"output_tokens": 1_000_000}, "claude-haiku-4-5") == 5.0
    # a variant of a known id (the CLI may add a context suffix) is priced as that model
    assert step_cost(MTOK, "claude-sonnet-5-5[1m]") == 2.0
    most = max(PRICES.values(), key=lambda p: p["output_tokens"])
    assert step_cost(MTOK, "claude-new-9") == most["input_tokens"]
    assert step_cost(MTOK, None) == most["input_tokens"]


def test_the_runtime_defaults_to_the_setting_which_the_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWARMIFY_MODEL", raising=False)
    assert SdkRuntime().model == DEFAULT_MODEL
    monkeypatch.setenv("SWARMIFY_MODEL", "claude-haiku-4-5")
    assert SdkRuntime().model == "claude-haiku-4-5"
    assert SdkRuntime(model="claude-sonnet-5-5").model == "claude-sonnet-5-5"


def test_each_role_runs_on_its_cast_model_and_the_agent_records_it(
    board: SqliteStorage, tmp_path: Path
) -> None:
    board.add_task(W, R, S, "Pull top shorts", added_by=OPERATOR)
    work = [
        Call(tool_id("query_graph"), {"question": "?"}),
        Call(tool_id("claim_task")),
        Call(tool_id("finish_task"), {"task_id": LAST, "note": "Done."}),
        Say("Done."),
    ]
    script = Script(work, [Answer({"passed": True, "reasons": ["ok"]})], usage=MTOK)
    seen: list[float] = []

    def budget(run_id: str) -> str | None:
        seen.append(sum(a.cost_usd for a in board.list_agents(run_id)))
        return None

    runtime = SdkRuntime(transport=script.transport)
    swarm = Swarm(board, runtime, DiskFiles(tmp_path), budget=budget)  # type: ignore[arg-type]
    folder = tmp_path / W
    folder.mkdir()
    cast = [
        CastEntry("worker-1", "worker", model="claude-haiku-4-5"),
        CastEntry("judge", "judge", model="claude-sonnet-5-5"),
    ]
    swarm.run_stage(W, R, S, "research", {"goal": "g", "done_when": "d"}, folder, cast=cast)

    assert [o.model for o in script.sessions] == ["claude-haiku-4-5", "claude-sonnet-5-5"]
    agents = {a.name: a for a in board.list_agents(R)}
    assert agents["worker-1"].model == "claude-haiku-4-5"
    assert agents["judge"].model == "claude-sonnet-5-5"
    # each tool step reads a million input tokens, priced as Haiku's
    assert seen[1:4] == [1.0, 2.0, 3.0]
    started = [e.detail for e in board.list_log(S) if " started on " in e.detail]
    assert "on claude-haiku-4-5" in started[0] and "on claude-sonnet-5-5" in started[1]


def test_ask_runs_on_the_model_it_is_given(board: SqliteStorage, tmp_path: Path) -> None:
    script = Script([Answer({"n": 1})])
    swarm = Swarm(board, SdkRuntime(transport=script.transport), DiskFiles(tmp_path))  # type: ignore[arg-type]
    folder = tmp_path / W
    folder.mkdir()
    swarm.ask(W, R, S, "retro", folder, "retro", "sys", "prompt", {}, lambda o: o, model="claude-x")
    assert script.sessions[0].model == "claude-x"
    assert {a.name: a.model for a in board.list_agents(R)} == {"retro": "claude-x"}
