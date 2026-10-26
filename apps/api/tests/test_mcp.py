"""The swarmify MCP tools, driven through an MCP client connected in-process."""

from pathlib import Path

import pytest
from mcp import Client

from swarmify_api.main import world_service
from swarmify_api.mcp import server


@pytest.mark.anyio
async def test_world_tools_create_list_get_delete(tmp_path: Path) -> None:
    worlds = world_service(tmp_path)
    async with Client(server(worlds)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"create_world", "list_worlds", "get_world", "delete_world"} <= names

        created = await client.call_tool("create_world", {"name": "Video", "purpose": "Shorts."})
        assert not created.is_error
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
