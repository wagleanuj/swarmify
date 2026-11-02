"""The swarmify MCP tools, driven through an MCP client connected in-process."""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from api_fakes import studio
from mcp import Client, StdioServerParameters

from swarmify_api.auth import CALLER, Token
from swarmify_api.main import (
    create_app,
    run_control,
    task_board,
    workflow_service,
    world_deleter,
    world_service,
)
from swarmify_api.mcp import server
from swarmify_runs import Run, StageDef
from swarmify_swarm import tool_id
from swarmify_swarm.fake import Answer, Call, Script


@pytest.mark.anyio
async def test_world_tools_create_list_get_delete(tmp_path: Path) -> None:
    worlds = world_service(tmp_path)
    async with Client(
        server(worlds, workflow_service(tmp_path), task_board(tmp_path), run_control(tmp_path))
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
    worlds, workflows, control = (
        world_service(tmp_path),
        workflow_service(tmp_path),
        run_control(tmp_path),
    )
    delete = world_deleter(worlds, workflows, control)
    async with Client(server(worlds, workflows, task_board(tmp_path), control, delete)) as client:
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
            {"world_id": world.id, "yaml": "name: x\nstages:\n  - connector: remotion_build\n"},
        )
        assert not refused.is_error and refused.structured_content
        assert [v["rule"] for v in refused.structured_content["violations"]] == [
            "unknown_connector"  # a package this world was never given
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
    control.close()


@pytest.mark.anyio
async def test_task_tools_add_and_list_on_a_swarm_stage(tmp_path: Path) -> None:
    worlds, control, board = world_service(tmp_path), run_control(tmp_path), task_board(tmp_path)
    world = worlds.create("Video")
    run = control.storage.add_run(
        Run("run_1", world.id, "wf_1", 1, "Research", "queued", "t", "t", []),
        [StageDef("brief", "gate", {"ask": "Go?"}), StageDef("research", "swarm", {})],
    )
    gate, swarm = run.stages
    async with Client(server(worlds, workflow_service(tmp_path), board, control)) as client:
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


REVIEW = (
    "name: Review loop\nstages:\n  - connector: render\n    calls: studio\n"
    "    output: short.mp4\n"
    "  - gate: review\n    ask: Watch it.\n    kind: approve\n  - gate: publish_ok\n"
    "    ask: Publish?\n    kind: confirm\n  - connector: youtube\n    calls: youtube\n"
    "    input: short.mp4\n    params: {title: Hooks}\n"
)


@pytest.mark.anyio
async def test_gate_tools_act_on_runs_the_api_process_hosts(tmp_path: Path) -> None:
    """The MCP server runs in its own process; its control reaches runs through the shared
    DBOS system database. Here the API app in a TestClient plays the hosting process."""
    from api_fakes import until
    from fastapi.testclient import TestClient

    worlds = world_service(tmp_path)
    control = run_control(tmp_path)
    with TestClient(create_app(tmp_path, connector=lambda *_: None, retro=False)) as http:
        world = http.post("/api/worlds", json={"name": "Video"}).json()["id"]
        studio(http, world)
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": REVIEW}).json()
        run = http.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")

        async with Client(
            server(worlds, workflow_service(tmp_path), task_board(tmp_path), control)
        ) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            tools = {"list_gates", "decide_gate", "pause_run", "resume_run", "abort_run", "get_run"}
            assert tools <= names

            gates = await client.call_tool("list_gates")
            assert gates.structured_content
            (gate,) = gates.structured_content["result"]
            assert (gate["run_id"], gate["stage_name"]) == (run["id"], "review")
            assert gate["kind"] == "approve"
            assert gate["earlier_stages"] == ["render"] and gate["send_backs_left"] == 2

            back = await client.call_tool(
                "decide_gate", {"run_id": run["id"], "note": "Darker.", "send_back_to": "render"}
            )
            assert not back.is_error and back.structured_content
            assert back.structured_content["decisions"][0]["send_back_to"] == "render"
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["send_backs"] == 1)
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
            got = await client.call_tool("get_run", {"run_id": run["id"]})
            assert got.structured_content
            assert got.structured_content["stages"][0]["notes"] == ["Darker."]

            paused = await client.call_tool("pause_run", {"run_id": run["id"]})
            assert not paused.is_error
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "paused")
            assert (await client.call_tool("pause_run", {"run_id": run["id"]})).is_error
            resumed = await client.call_tool("resume_run", {"run_id": run["id"]})
            assert not resumed.is_error
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")

            approved = await client.call_tool("decide_gate", {"run_id": run["id"]})
            assert not approved.is_error
            stage = lambda i: http.get(f"/api/runs/{run['id']}").json()["stages"][i]  # noqa: E731
            until(lambda: stage(2)["status"] == "waiting")
            aborted = await client.call_tool("abort_run", {"run_id": run["id"]})
            assert not aborted.is_error
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "aborted")
            assert (await client.call_tool("abort_run", {"run_id": run["id"]})).is_error
            assert (await client.call_tool("decide_gate", {"run_id": "run_nope"})).is_error
            assert (await client.call_tool("get_run", {"run_id": "run_nope"})).is_error
            assert (await client.call_tool("list_gates")).structured_content == {"result": []}
    control.close()


PICK = (
    "name: Pick loop\nstages:\n  - connector: research\n    calls: studio\n"
    "  - gate: pick\n    ask: Pick one.\n    kind: pick\n"
)
IDEA = '---\ntype: "idea"\ntitle: "Hooks"\nrank: 1\n---\n\n# Hooks\n\nOpen on the hook.\n'


@pytest.mark.anyio
async def test_a_pick_gate_takes_an_artifact_path_as_the_choice(tmp_path: Path) -> None:
    """Story 53 over MCP: the choice is the chosen card's path; the decision records it and
    takes the card's title; a path from outside the run's earlier stages is refused."""
    from api_fakes import until
    from fastapi.testclient import TestClient

    worlds = world_service(tmp_path)
    control = run_control(tmp_path)
    with TestClient(create_app(tmp_path, connector=lambda *_: None, retro=False)) as http:
        world = http.post("/api/worlds", json={"name": "Video"}).json()["id"]
        studio(http, world)
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": PICK}).json()
        run = http.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
        research, gate = (s["id"] for s in run["stages"])
        card = tmp_path / "worlds" / world / "runs" / run["id"] / research / "artifacts"
        card.mkdir(parents=True)
        (card / "idea-hooks.md").write_text(IDEA)
        path = f"runs/{run['id']}/{research}/artifacts/idea-hooks.md"

        async with Client(
            server(worlds, workflow_service(tmp_path), task_board(tmp_path), control)
        ) as client:
            wrong = {"run_id": run["id"], "artifact": f"runs/{run['id']}/{gate}/decision.md"}
            assert (await client.call_tool("decide_gate", wrong)).is_error
            assert (await client.call_tool("decide_gate", {"run_id": run["id"]})).is_error
            picked = await client.call_tool("decide_gate", {"run_id": run["id"], "artifact": path})
            assert not picked.is_error and picked.structured_content
            (decision,) = picked.structured_content["decisions"]
            assert (decision["choice"], decision["artifact"]) == ("Hooks", path)
            assert 'artifact: "runs/' in Path(decision["path"]).read_text()
            until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "done")
    control.close()


@pytest.mark.anyio
async def test_the_stdio_server_deletes_a_world_like_the_api_does(tmp_path: Path) -> None:
    """Story 7: deleting through the MCP process cancels the world's live run in the hosting
    process and drops its Slack channel and learning rows, the same delete HTTP runs."""
    import sqlite3
    from contextlib import closing

    from api_fakes import until
    from fastapi.testclient import TestClient

    from swarmify_api.mcp import stdio_server

    control = run_control(tmp_path)
    with TestClient(create_app(tmp_path, connector=lambda *_: None, retro=False)) as http:
        world = http.post("/api/worlds", json={"name": "Video"}).json()["id"]
        studio(http, world)
        hook = {"slack_webhook": "https://hooks.slack.com/services/T/B/x"}
        assert http.put(f"/api/worlds/{world}/notifications", json=hook).status_code < 300
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": REVIEW}).json()
        run = http.post(f"/api/workflows/{wf['workflow_id']}/runs", json={}).json()
        until(lambda: http.get(f"/api/runs/{run['id']}").json()["status"] == "waiting_gate")
        with closing(sqlite3.connect(tmp_path / "swarmify.db")) as conn:
            conn.execute(
                "INSERT INTO skill_proposals (id, world_id, run_id, stage_id, status, path,"
                " created_at) VALUES ('prop_1', ?, ?, 's1', 'pending', 'p.md', 't')",
                (world, run["id"]),
            )
            conn.commit()

        async with Client(stdio_server(tmp_path, control)) as client:
            deleted = await client.call_tool("delete_world", {"world_id": world})
            assert deleted.structured_content == {"deleted": world}

        status = control.client.retrieve_workflow(run["id"]).get_status()
        assert status is not None and status.status == "CANCELLED"
        with closing(sqlite3.connect(tmp_path / "swarmify.db")) as conn:
            assert conn.execute("SELECT count(*) FROM channels").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM skill_proposals").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    control.close()


@pytest.mark.anyio
async def test_mcp_starts_a_run_lists_its_artifacts_and_forks_it(tmp_path: Path) -> None:
    """Story 89 from the MCP process, which hosts no DBOS: it records the run queued and the
    API process adopts it on its next sweep."""
    from api_fakes import until
    from fastapi.testclient import TestClient

    control = run_control(tmp_path)
    app = create_app(tmp_path, connector=lambda *_: None, retro=False, sweep_seconds=0.05)
    with TestClient(app) as http:
        world = http.post("/api/worlds", json={"name": "Video"}).json()["id"]
        studio(http, world)
        wf = http.post(f"/api/worlds/{world}/workflows", json={"yaml": PICK}).json()
        status = lambda r: http.get(f"/api/runs/{r}").json()["status"]  # noqa: E731

        async with Client(
            server(
                world_service(tmp_path),
                workflow_service(tmp_path),
                task_board(tmp_path),
                control,
                retro=False,
            )
        ) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"start_run", "list_artifacts", "fork_run"} <= names

            started = await client.call_tool(
                "start_run", {"workflow_id": wf["workflow_id"], "budget_usd": 3}
            )
            run = started.structured_content
            assert not started.is_error and run
            assert (run["status"], run["budget_usd"]) == ("queued", 3)
            assert [s["name"] for s in run["stages"]] == ["research", "pick"]
            until(lambda: status(run["id"]) == "waiting_gate")

            research = run["stages"][0]["id"]
            card = tmp_path / "worlds" / world / "runs" / run["id"] / research / "artifacts"
            card.mkdir(parents=True)
            (card / "idea-hooks.md").write_text(IDEA)
            path = f"runs/{run['id']}/{research}/artifacts/idea-hooks.md"
            listed = await client.call_tool("list_artifacts", {"run_id": run["id"]})
            assert listed.structured_content
            assert [(a["path"], a["title"]) for a in listed.structured_content["result"]] == [
                (path, "Hooks")
            ]
            await client.call_tool("decide_gate", {"run_id": run["id"], "artifact": path})
            until(lambda: status(run["id"]) == "done")

            forked = await client.call_tool("fork_run", {"run_id": run["id"], "stage": 1})
            fork = forked.structured_content
            assert not forked.is_error and fork and fork["id"] != run["id"]
            until(lambda: status(fork["id"]) == "waiting_gate")
            copied = await client.call_tool("list_artifacts", {"run_id": fork["id"]})
            assert copied.structured_content
            assert [a["title"] for a in copied.structured_content["result"]] == ["Hooks"]

            assert (await client.call_tool("start_run", {"workflow_id": "wf_nope"})).is_error
            assert (await client.call_tool("fork_run", {"run_id": "run_nope", "stage": 1})).is_error
            assert (await client.call_tool("fork_run", {"run_id": run["id"], "stage": 9})).is_error
            assert (await client.call_tool("list_artifacts", {"run_id": "run_nope"})).is_error
    control.close()


def tools_of(app: Any) -> Any:
    """The MCP server the app serves at /mcp, for an in-process client."""
    return next(r.app.tools for r in app.router.routes if getattr(r, "path", None) == "/mcp")


@contextmanager
def as_token(name: str, world_id: str | None = None) -> Iterator[None]:
    """Call the tools as the API token `name`, as /mcp does for a Bearer token."""
    reset = CALLER.set(Token(id="tok_1", name=name, created_at="t", world_id=world_id))
    try:
        yield
    finally:
        CALLER.reset(reset)


@pytest.mark.anyio
async def test_an_mcp_client_reads_a_run_proposes_applies_and_lists_its_changes(
    app: Any, script: Script, tmp_path: Path
) -> None:
    """Stories 54 and 55: a token's client reads the run with its stages' config and revision,
    proposes a Stage Edit, applies it with the revision, recorded as made by the token's name,
    and lists it; a stale apply is refused; ask_run is one turn of the Run Assistant."""
    from fastapi.testclient import TestClient
    from test_run_changes_http import at_gate, change, stage

    with TestClient(app) as http, as_token("Claude Code on the laptop"):
        run = at_gate(http)
        pick = stage(run, "pick")["id"]
        notes = tmp_path / "worlds" / run["world_id"] / "runs" / run["id"] / pick / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("Idea 1 won on hooks.")
        edit = change(run, "render", params={"fps": 60})
        script.scripts = [
            [
                Call(tool_id("propose_change"), change(run, "tag", params={"fps": 24})),
                Answer({"reply": "Tag at 24.", "sources": [], "proposals": []}),
            ]
        ]

        async with Client(tools_of(app)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"read_run_file", "propose_run_change", "apply_run_change"} <= names
            assert {"list_run_changes", "ask_run"} <= names

            got = (await client.call_tool("get_run", {"run_id": run["id"]})).structured_content
            assert got and got["revision"] == run["revision"]
            assert stage(got, "render")["config"]["params"] == {"fps": 30}

            path = f"runs/{run['id']}/{pick}/notes.md"
            read = await client.call_tool("read_run_file", {"run_id": run["id"], "path": path})
            assert not read.is_error and read.structured_content == {
                "result": "Idea 1 won on hooks."
            }
            escaped = {"run_id": run["id"], "path": f"runs/{run['id']}/../../../swarmify.db"}
            assert (await client.call_tool("read_run_file", escaped)).is_error

            proposed = await client.call_tool("propose_run_change", {"run_id": run["id"], **edit})
            proposal = proposed.structured_content
            assert not proposed.is_error and proposal
            assert (proposal["before"]["params"], proposal["after"]["params"]) == (
                {"fps": 30},
                {"fps": 60},
            )
            assert proposal["violations"] == [] and proposal["revision"] == run["revision"]

            apply = {"run_id": run["id"], **edit, "revision": proposal["revision"]}
            applied = await client.call_tool("apply_run_change", apply)
            assert not applied.is_error and applied.structured_content
            assert applied.structured_content["by"] == "Claude Code on the laptop"
            stale = await client.call_tool("apply_run_change", apply)
            assert stale.is_error and "moved on" in str(stale.content)

            listed = await client.call_tool("list_run_changes", {"run_id": run["id"]})
            assert listed.structured_content
            assert [(c["kind"], c["by"]) for c in listed.structured_content["result"]] == [
                ("stage_edit", "Claude Code on the laptop")
            ]
            assert (
                http.get(f"/api/runs/{run['id']}/changes").json()
                == (listed.structured_content["result"])
            )

            asked = await client.call_tool("ask_run", {"run_id": run["id"], "message": "Tag?"})
            reply = asked.structured_content
            assert not asked.is_error and reply and reply["text"] == "Tag at 24."
            [card] = reply["proposals"]
            assert card["proposal"]["after"]["params"] == {"fps": 24}
            conversation = http.get(f"/api/runs/{run['id']}/assistant").json()["messages"]
            assert [m["text"] for m in conversation] == ["Tag?", "Tag at 24."]
            assert (await client.call_tool("list_run_changes", {"run_id": "run_nope"})).is_error


@pytest.mark.anyio
async def test_a_token_scoped_to_one_world_is_refused_on_another_worlds_run(
    app: Any, script: Script
) -> None:
    """Story 56: a token scoped to one world reads and changes its runs, and is refused on every
    run, world and workflow of another."""
    from fastapi.testclient import TestClient
    from test_run_changes_http import at_gate, change

    with TestClient(app) as http:
        mine, theirs = at_gate(http), at_gate(http)
        theirs_wf = http.get(f"/api/worlds/{theirs['world_id']}/workflows").json()[0]["id"]
        edit = change(theirs, "render", params={"fps": 60})
        with as_token("Scoped", world_id=mine["world_id"]):
            async with Client(tools_of(app)) as client:
                own = await client.call_tool("get_run", {"run_id": mine["id"]})
                assert not own.is_error
                proposed = await client.call_tool(
                    "propose_run_change", {"run_id": mine["id"], **change(mine, "tag")}
                )
                assert not proposed.is_error
                worlds = (await client.call_tool("list_worlds")).structured_content
                assert worlds and [w["id"] for w in worlds["result"]] == [mine["world_id"]]
                gates = (await client.call_tool("list_gates")).structured_content
                assert gates and [g["run_id"] for g in gates["result"]] == [mine["id"]]

                run = {"run_id": theirs["id"]}
                refused = [
                    ("get_run", run),
                    ("list_artifacts", run),
                    ("read_run_file", {**run, "path": f"runs/{theirs['id']}/x.md"}),
                    ("propose_run_change", {**run, **edit}),
                    ("apply_run_change", {**run, **edit, "revision": theirs["revision"]}),
                    ("list_run_changes", run),
                    ("ask_run", {**run, "message": "Hi"}),
                    ("decide_gate", {**run, "choice": "idea 1"}),
                    ("abort_run", run),
                    ("fork_run", {**run, "stage": 1}),
                    ("get_world", {"world_id": theirs["world_id"]}),
                    ("delete_world", {"world_id": theirs["world_id"]}),
                    ("list_workflows", {"world_id": theirs["world_id"]}),
                    ("get_workflow", {"workflow_id": theirs_wf}),
                    ("start_run", {"workflow_id": theirs_wf}),
                    ("create_world", {"name": "Another"}),
                ]
                for tool, args in refused:
                    answer = await client.call_tool(tool, args)
                    assert answer.is_error, tool
                    assert "reaches only" in str(answer.content), tool
        assert script.sessions == []
        after = http.get(f"/api/runs/{theirs['id']}").json()
        assert after["status"] == "waiting_gate" and after["changes"] == []
        assert len(http.get("/api/worlds").json()) == 2


@pytest.mark.anyio
async def test_a_chat_threads_replies_ask_the_run_assistant_and_apply_or_dismiss_its_proposals(
    app: Any, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 315, the core side of remote approvals: only a world whose remote approver offers
    run_assistant is answered; a proposal is applied as `by`, once, or dismissed with the run
    untouched; another run's proposal is not found; a run that moved on answers MOVED_ON, also
    when it moves between reading the card and applying it."""
    from fastapi.testclient import TestClient
    from test_run_changes_http import at_gate, change

    from swarmify_api import remote
    from swarmify_api.assistant import MOVED_ON, Conversations
    from swarmify_connectors import Connectors

    approvals = next(
        r.app.tools for r in app.router.routes if getattr(r, "path", "") == remote.PATH
    )
    with TestClient(app) as http:
        run, other = at_gate(http), at_gate(http)
        rid = {"run_id": run["id"]}
        script.scripts = [
            [Call(tool_id("propose_change"), change(run, "render", params={"fps": n})), Answer(a)]
            for n in (60, 24, 50, 40)
            for a in [{"reply": f"At {n}.", "sources": []}]
        ]

        def ask(client: Client, message: str) -> Any:
            return client.call_tool("ask_run", {**rid, "message": message})

        def cards() -> list[tuple[str, str | None]]:
            said = http.get(f"/api/runs/{run['id']}/assistant").json()["messages"]
            return [(c["state"], c["change_id"]) for m in said for c in m["proposals"]]

        async with Client(approvals) as client:
            refused = await ask(client, "Faster?")
            assert refused.is_error and "offers the run_assistant" in str(refused.content)

            slack = SimpleNamespace(
                manifest={"capabilities": ["remote_approvals", "run_assistant"]}
            )
            monkeypatch.setattr(Connectors, "remote_approvers", lambda *_: [slack])
            said = (await ask(client, "Faster?")).structured_content
            assert said and said["reply"] == "At 60."
            [first] = said["proposals"]
            assert (first["title"], first["violations"], first["in_place"], first["ends"]) == (
                "Edit render", [], True, False
            )  # fmt: skip
            one = {**rid, "proposal_id": first["id"]}
            assert (await client.call_tool("apply_proposal", {**one, "by": " "})).is_error
            applied = await client.call_tool("apply_proposal", {**one, "by": "slack:lee"})
            assert applied.structured_content == {"id": first["id"], "state": "applied"}
            [made] = http.get(f"/api/runs/{run['id']}/changes").json()
            assert made["by"] == "slack:lee" and cards() == [("applied", made["id"])]
            again = await client.call_tool("apply_proposal", {**one, "by": "slack:lee"})
            assert again.is_error and "already applied" in str(again.content)
            theirs = {"run_id": other["id"], "proposal_id": first["id"], "by": "slack:lee"}
            assert "proposal not found" in str(
                (await client.call_tool("apply_proposal", theirs)).content
            )

            said = (await ask(client, "Slower?")).structured_content
            two = {**rid, "proposal_id": said and said["proposals"][0]["id"]}
            dismissed = await client.call_tool("dismiss_proposal", two)
            assert (
                dismissed.structured_content
                and dismissed.structured_content["state"] == "dismissed"
            )
            assert (await client.call_tool("dismiss_proposal", two)).is_error
            assert len(http.get(f"/api/runs/{run['id']}/changes").json()) == 1

            def bump() -> None:
                now = http.get(f"/api/runs/{run['id']}").json()
                edit = {
                    **change(now, "tag", params={"n": now["revision"]}),
                    "revision": now["revision"],
                }
                http.post(f"/api/runs/{run['id']}/changes", json=edit).raise_for_status()

            said = (await ask(client, "50?")).structured_content
            bump()  # the card reads as stale
            stale = {**rid, "proposal_id": said and said["proposals"][0]["id"], "by": "slack:lee"}
            moved = await client.call_tool("apply_proposal", stale)
            assert moved.is_error and MOVED_ON in str(moved.content)

            said = (await ask(client, "40?")).structured_content
            read = Conversations.card

            def then_moved(self: Conversations, *args: Any) -> Any:
                card = read(self, *args)
                bump()  # the run moves after the card was read open
                return card

            monkeypatch.setattr(Conversations, "card", then_moved)
            raced = {**rid, "proposal_id": said and said["proposals"][0]["id"], "by": "slack:lee"}
            moved = await client.call_tool("apply_proposal", raced)
            assert moved.is_error and MOVED_ON in str(moved.content)
        assert [c["by"] for c in http.get(f"/api/runs/{run['id']}/changes").json()] == [
            "slack:lee", "operator", "operator"
        ]  # fmt: skip
