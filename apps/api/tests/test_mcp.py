"""The swarmify MCP tools, driven through an MCP client connected in-process."""

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from swarmify_api.main import run_storage, task_board, workflow_service, world_service
from swarmify_api.mcp import server
from swarmify_runs import Run, StageDef


@pytest.mark.anyio
async def test_world_tools_create_list_get_delete(tmp_path: Path) -> None:
    worlds = world_service(tmp_path)
    async with Client(
        server(
            worlds, workflow_service(tmp_path), task_board(tmp_path), run_storage(tmp_path).get_run
        )
    ) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"create_world", "list_worlds", "get_world", "delete_world"} <= names

        created = await client.call_tool("create_world", {"name": "Video", "purpose": "Shorts."})
        assert not created.is_error
        assert (await client.call_tool("create_world", {"name": "  "})).is_error
        world = created.structured_content
        assert world and world["name"] == "Video" and world["purpose"] == "Shorts."
        assert (tmp_path / "worlds" / world["id"]).is_dir()

        listed = await client.call_tool("list_worlds")
        assert listed.structured_content == {"result": [world]}

        got = await client.call_tool("get_world", {"world_id": world["id"]})
        assert got.structured_content == world

        deleted = await client.call_tool("delete_world", {"world_id": world["id"]})
        assert deleted.structured_content == {"deleted": world["id"]}
        assert not (tmp_path / "worlds" / world["id"]).exists()
        assert [e.type for e in worlds.events_after(world["id"])] == [
            "world.created",
            "world.deleted",
        ]

        missing = await client.call_tool("get_world", {"world_id": "world_nope"})
        assert missing.is_error
        assert (await client.call_tool("delete_world", {"world_id": "world_nope"})).is_error


@pytest.mark.anyio
async def test_console_script_serves_the_tools_over_stdio(tmp_path: Path) -> None:
    script = Path(sys.executable).parent / "swarmify-mcp"
    env = {**os.environ, "SWARMIFY_DATA_DIR": str(tmp_path)}
    async with Client(StdioServerParameters(command=str(script), env=env)) as client:
        created = await client.call_tool("create_world", {"name": "Video"})
        assert created.structured_content and created.structured_content["name"] == "Video"
    assert (tmp_path / "swarmify.db").is_file()


WEEKLY = (
    "name: Weekly short\nstages:\n  - swarm: research\n    goal: Find ideas.\n"
    "    done_when: 5 ideas\n    tools: [web_search]\n  - gate: pick\n    ask: Pick one.\n"
)


@pytest.mark.anyio
async def test_workflow_tools_save_check_list_get(tmp_path: Path) -> None:
    worlds = world_service(tmp_path)
    async with Client(
        server(
            worlds, workflow_service(tmp_path), task_board(tmp_path), run_storage(tmp_path).get_run
        )
    ) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"save_workflow", "check_workflow", "list_workflows", "get_workflow"} <= names
        world = worlds.create("Video")

        checked = await client.call_tool(
            "check_workflow",
            {"world_id": world.id, "yaml": "name: x\nstages:\n  - connector: nope\n"},
        )
        assert checked.structured_content
        assert [v["rule"] for v in checked.structured_content["violations"]] == [
            "unknown_connector"
        ]
        assert checked.structured_content["model"]["stages"][0]["calls"] == "nope"

        refused = await client.call_tool(
            "save_workflow",
            {"world_id": world.id, "yaml": "name: x\nstages:\n  - connector: youtube\n"},
        )
        assert not refused.is_error and refused.structured_content
        assert [v["rule"] for v in refused.structured_content["violations"]] == [
            "publish_without_gate"
        ]
        assert (
            await client.call_tool("list_workflows", {"world_id": world.id})
        ).structured_content == {"result": []}

        saved = await client.call_tool("save_workflow", {"world_id": world.id, "yaml": WEEKLY})
        assert not saved.is_error and saved.structured_content
        v1 = saved.structured_content
        assert v1["number"] == 1 and v1["model"]["name"] == "Weekly short"
        again = await client.call_tool(
            "save_workflow",
            {
                "world_id": world.id,
                "yaml": WEEKLY.replace("Weekly", "Daily"),
                "workflow_id": v1["workflow_id"],
            },
        )
        assert again.structured_content and again.structured_content["number"] == 2

        listed = await client.call_tool("list_workflows", {"world_id": world.id})
        assert listed.structured_content and [
            (w["name"], w["current_version"]) for w in listed.structured_content["result"]
        ] == [("Daily short", 2)]
        latest = await client.call_tool("get_workflow", {"workflow_id": v1["workflow_id"]})
        assert latest.structured_content and latest.structured_content["number"] == 2
        first = await client.call_tool(
            "get_workflow", {"workflow_id": v1["workflow_id"], "version": 1}
        )
        assert first.structured_content == v1

        assert (await client.call_tool("get_workflow", {"workflow_id": "wf_nope"})).is_error
        assert (
            await client.call_tool("get_workflow", {"workflow_id": v1["workflow_id"], "version": 9})
        ).is_error
        assert (
            await client.call_tool("save_workflow", {"world_id": "world_nope", "yaml": WEEKLY})
        ).is_error
        assert (
            await client.call_tool(
                "save_workflow", {"world_id": world.id, "yaml": WEEKLY, "workflow_id": "wf_nope"}
            )
        ).is_error

        await client.call_tool("delete_world", {"world_id": world.id})
        assert (await client.call_tool("get_workflow", {"workflow_id": v1["workflow_id"]})).is_error


@pytest.mark.anyio
async def test_task_tools_add_and_list_on_a_swarm_stage(tmp_path: Path) -> None:
    worlds, runs, board = world_service(tmp_path), run_storage(tmp_path), task_board(tmp_path)
    world = worlds.create("Video")
    run = runs.add_run(
        Run("run_1", world.id, "wf_1", 1, "Research", "queued", "t", "t", []),
        [StageDef("brief", "gate", {"ask": "Go?"}), StageDef("research", "swarm", {})],
    )
    gate, swarm = run.stages
    async with Client(server(worlds, workflow_service(tmp_path), board, runs.get_run)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"add_task", "list_tasks"} <= names
        added = await client.call_tool(
            "add_task", {"run_id": run.id, "stage_id": swarm.id, "title": "Check the niche"}
        )
        task = added.structured_content
        assert not added.is_error and task
        assert (task["status"], task["added_by"], task["stage_id"]) == (
            "open",
            "operator",
            swarm.id,
        )
        listed = await client.call_tool("list_tasks", {"run_id": run.id, "stage_id": swarm.id})
        assert listed.structured_content == {"result": [task]}
        assert board.list_tasks(swarm.id)[0].id == task["id"]

        on_gate = {"run_id": run.id, "stage_id": gate.id, "title": "x"}
        assert (await client.call_tool("add_task", on_gate)).is_error
        unknown = {"run_id": run.id, "stage_id": "stage_nope", "title": "x"}
        assert (await client.call_tool("add_task", unknown)).is_error
        assert (
            await client.call_tool("list_tasks", {"run_id": "run_nope", "stage_id": swarm.id})
        ).is_error
