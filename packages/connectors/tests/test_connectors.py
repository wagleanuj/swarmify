"""The Connectors service at its seam: real SQLite, a recording fake transport behind it."""

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import (
    Connector,
    ConnectorError,
    Connectors,
    FakeTransport,
    KeyCheck,
    Tool,
)
from swarmify_kernel.events import read_after
from swarmify_kernel.sqlite import connect

W = "world_1"
TOOLS = [Tool("fal_video", "Make a clip.", {"type": "object"})]


def fal(connectors: Connectors, **over: Any) -> Connector:
    fields: dict[str, Any] = {
        "name": "fal_video",
        "kind": "stage",
        "server": "uvx fal-mcp --model 'kling 2'",
        "credentials": ["FAL_KEY"],
        "stage_types": ["connector"],
    }
    return connectors.draft(**{"world_id": W, **fields, **over})


def events(db: Path) -> list[dict[str, Any]]:
    with closing(connect(db)) as conn:
        return [e.payload for e in read_after(conn, W, 0) if e.type == "connector_call.updated"]


def test_web_search_and_the_graph_are_agent_tools_not_connectors(connectors: Connectors) -> None:
    names = {c.name for c in connectors.list_all(W)}
    assert names.isdisjoint({"web_search", "graph"})
    assert connectors.get("builtin_graph") is None


def test_a_draft_reads_a_command_or_a_url(connectors: Connectors) -> None:
    command = fal(connectors)
    assert command.command == ["uvx", "fal-mcp", "--model", "kling 2"] and command.url is None
    url = fal(connectors, server=" https://mcp.example/mcp ")
    assert url.url == "https://mcp.example/mcp" and url.command == []


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"name": "Fal Video"}, "lower_snake_case"),
        ({"name": "youtube"}, "built-in"),
        ({"name": "graph"}, "Agent Tool"),
        ({"kind": "plugin"}, "kind is one of"),
        ({"server": "  "}, "MCP command or a URL"),
        ({"server": "uvx 'unclosed"}, "cannot read the command"),
        ({"credentials": ["fal-key"]}, "UPPER_SNAKE_CASE"),
        ({"stage_types": []}, "allowed stage types"),
        ({"stage_types": ["gate"]}, "allowed stage types"),
        ({"kind": "publish", "stage_types": ["swarm", "connector"]}, "after a gate"),
    ],
)
def test_a_bad_draft_names_its_problem(
    connectors: Connectors, over: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        fal(connectors, **over)


def test_a_connector_may_declare_input_types_and_they_are_kept(connectors: Connectors) -> None:
    recipe = {"type": "object", "required": ["steps"]}
    cook = fal(connectors, declared={"recipe": recipe}, input_types=["recipe", None])
    saved = connectors.add(cook, {})
    assert saved.types == {"recipe": recipe} and saved.input_types == ["recipe", None]
    assert connectors.type_schema(W, "recipe") == recipe
    assert connectors.type_schema(W, "idea") is None
    with pytest.raises(ValueError, match="a type name is lower_snake_case"):
        fal(connectors, name="other", declared={"Recipe": recipe})
    with pytest.raises(ValueError, match="type recipe is not a JSON Schema"):
        fal(connectors, name="other", declared={"recipe": {"type": "soup"}})
    with pytest.raises(ValueError, match="input types it does not declare: script"):
        fal(connectors, name="other", input_types=["script"])


# World-dependent types (issue 140), on a fixture publish connector whose `post` type lists
# the channels a world has keys for: nothing here is about speech.
POST = {"type": "object", "properties": {"channel": {"type": "string"}}, "required": ["channel"]}


def poster(connectors: Connectors) -> Connector:
    return connectors.add(
        fal(
            connectors,
            name="poster",
            credentials=[],
            declared={"post": POST},
            input_types=["post"],
            world_types="post_types",
        ),
        {},
    )


def test_a_connector_answers_its_types_for_a_world(
    connectors: Connectors, transport: FakeTransport
) -> None:
    channels = {**POST, "properties": {"channel": {"type": "string", "enum": ["news", "kids"]}}}
    transport.outputs["post_types"] = [{"types": {"post": channels}, "note": "Two channels."}]
    connector = poster(connectors)
    assert connectors.find(W, "poster") == connector and connector.world_types == "post_types"
    assert connectors.for_world(W, connector) == ({"post": channels}, "Two channels.")
    assert connectors.type_schema(W, "post") == channels
    ((_, tool, args, _),) = [c for c in transport.calls if c[0] == "poster"][:1]
    assert tool == "post_types" and args["world"] == W
    assert args["assets_path"] == str(connectors.data_dir / "worlds" / W / "assets")


@pytest.mark.parametrize(
    "answer",
    [
        ConnectorError("poster: could not start"),
        {"ok": True},
        {"types": "post"},
        {"types": {"post": {"type": "soup"}}},
        {"types": {"other": POST}},  # only a type it declares may change
    ],
)
def test_a_connector_that_cannot_answer_for_the_world_keeps_its_declared_types(
    connectors: Connectors, transport: FakeTransport, answer: Any
) -> None:
    transport.outputs["post_types"] = [answer]
    connector = poster(connectors)
    assert connectors.for_world(W, connector) == ({"post": POST}, "")
    assert connectors.type_schema(W, "post") == POST


def test_a_connector_without_world_types_is_never_asked(
    connectors: Connectors, transport: FakeTransport
) -> None:
    cook = connectors.add(fal(connectors, credentials=[], declared={"recipe": POST}), {})
    assert connectors.for_world(W, cook) == ({"recipe": POST}, "")
    assert all(c[0] != "fal_video" for c in transport.calls)


def test_test_starts_the_server_once_with_only_its_credentials_and_saves_nothing(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.credentials.set({"FAL_KEY": "from-file", "OTHER": "never-sent"})
    transport.tools["fal_video"] = TOOLS
    draft = fal(connectors)
    assert connectors.test(draft, {}) == TOOLS
    assert connectors.test(draft, {"FAL_KEY": "typed"}) == TOOLS
    assert transport.starts == [
        ("fal_video", {"FAL_KEY": "from-file"}),
        ("fal_video", {"FAL_KEY": "typed"}),
    ]
    assert connectors.credentials.read()["FAL_KEY"] == "from-file"
    assert connectors.find(W, "fal_video") is None


def test_test_without_a_credential_says_which_one(connectors: Connectors) -> None:
    with pytest.raises(ConnectorError, match="needs FAL_KEY"):
        connectors.test(fal(connectors), {})


def test_add_installs_a_local_package_enabled_for_the_world_with_its_keys_as_credentials(
    connectors: Connectors, db: Path
) -> None:
    saved = connectors.add(fal(connectors), {"FAL_KEY": "s3cret", "STRAY": "ignored"})
    assert connectors.find(W, "fal_video") == saved
    assert (saved.version, saved.world_id, saved.builtin) == ("0.0.1", W, False)
    assert connectors.enabled(W) == {"fal_video": "0.0.1"}
    assert connectors.list_all("world_2")[-1].name != "fal_video"
    (package,) = connectors.packages.list()
    assert (package.name, package.source, package.state) == ("fal_video", "local", "installed")
    m = saved.manifest
    assert (m["kind"], m["stage_types"], m["runtime"]) == ("stage", ["connector"], "uv")
    assert m["command"] == ["uvx", "fal-mcp", "--model", "kling 2"]
    assert m["config"] == [
        {"key": "fal_key", "label": "FAL_KEY", "type": "string", "secret": True}
        | {"required": True, "scope": "shared", "env": "FAL_KEY"}
    ]
    assert connectors.setups.values("fal_video", W) == ({"fal_key": "s3cret"}, {})
    assert connectors.credentials.read() == {}
    assert b"s3cret" not in db.read_bytes()
    assert connectors.missing(saved) == []
    with pytest.raises(ValueError, match="a Connector Package this world enables already"):
        fal(connectors)

    # Another world adding the same server reuses the version; a different one is the next.
    assert connectors.add(fal(connectors, world_id="world_2"), {}).version == "0.0.1"
    other = connectors.add(fal(connectors, world_id="world_3", server="uvx fal-mcp"), {})
    assert other.version == "0.0.2"
    assert connectors.setups.values("fal_video", "world_3") == ({"fal_key": "s3cret"}, {})


def test_agent_servers_hand_a_swarm_stage_its_connectors_with_credentials(
    connectors: Connectors,
) -> None:
    connectors.add(fal(connectors, kind="tool", stage_types=["swarm"]), {"FAL_KEY": "s3cret"})
    remote = connectors.draft(W, "notes_api", "tool", "https://x.test/mcp", [], ["swarm"])
    connectors.add(remote, {})
    servers = connectors.agent_servers(W, ["fal_video", "notes_api", "web_search", "nope"])
    assert servers == {
        "fal_video": {
            "type": "stdio",
            "command": "uvx",
            "args": ["fal-mcp", "--model", "kling 2"],
            "env": {"FAL_KEY": "s3cret"},
        },
        "notes_api": {"type": "http", "url": "https://x.test/mcp", "headers": {}},
    }
    connectors.setups.path.write_text("{}")
    with pytest.raises(ConnectorError, match="needs its Setup"):
        connectors.agent_servers(W, ["fal_video"])


def test_missing_lists_the_keys_its_setup_lacks(connectors: Connectors) -> None:
    saved = connectors.add(fal(connectors, credentials=["FAL_KEY", "FAL_ORG"]), {"FAL_KEY": "k"})
    assert connectors.missing(saved) == ["fal_org"]
    connectors.setups.path.chmod(0o644)
    assert connectors.missing(saved) == ["fal_key", "fal_org"]
    with pytest.raises(PermissionError):
        connectors.add(fal(connectors, name="other"), {})
    assert connectors.find(W, "other") is None


YOUTUBE = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}


def test_saving_a_builtins_keys_checks_them_live_and_names_the_account(
    connectors: Connectors, transport: FakeTransport
) -> None:
    youtube = connectors.get("builtin_youtube")
    assert youtube
    transport.outputs["channel"] = [{"channel": "Hooks Daily"}]
    check = connectors.set_credentials(youtube, YOUTUBE)
    assert check == KeyCheck(True, "Posts to the YouTube channel Hooks Daily.")
    assert transport.calls[-1] == ("youtube", "channel", {}, YOUTUBE)


def test_a_failing_key_check_still_saves_and_says_why(
    connectors: Connectors, transport: FakeTransport
) -> None:
    youtube = connectors.get("builtin_youtube")
    assert youtube
    transport.outputs["channel"] = [ConnectorError("refreshing the YouTube token failed (400)")]
    check = connectors.set_credentials(youtube, YOUTUBE)
    assert check == KeyCheck(False, "refreshing the YouTube token failed (400)")
    assert connectors.missing(youtube) == []


def test_no_key_check_while_keys_are_missing_or_for_added_connectors(
    connectors: Connectors, transport: FakeTransport
) -> None:
    youtube = connectors.get("builtin_youtube")
    assert youtube
    assert connectors.set_credentials(youtube, {"YOUTUBE_CLIENT_ID": "id"}) is None
    added = connectors.add(fal(connectors), {})
    assert connectors.set_credentials(added, {"FAL_KEY": "k"}) is None
    assert transport.calls == []


STAGE = {"calls": "fal_video", "input": "script.md", "output": "clip.mp4", "params": {"len": 5}}


def test_a_stage_calls_its_tool_once_with_the_stage_id_as_idempotency_key(
    connectors: Connectors, transport: FakeTransport, db: Path
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [{"url": "https://cdn/clip.mp4"}]
    call = connectors.run_stage(W, "run_1", "stage_1", STAGE)
    request = {"len": 5, "idempotency_key": "stage_1", "input": "script.md", "output": "clip.mp4"}
    assert transport.calls == [("fal_video", "fal_video", request, {"FAL_KEY": "k"})]
    assert (call.status, call.request, call.response) == (
        "done",
        request,
        transport.outputs["fal_video"][0],
    )
    assert connectors.call_for("stage_1") == call

    again = connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert again == call and len(transport.calls) == 1
    assert [e["status"] for e in events(db)] == ["running", "done"]


def test_params_tool_picks_another_tool(connectors: Connectors, transport: FakeTransport) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    connectors.run_stage(W, "run_1", "stage_1", {"calls": "fal_video", "params": {"tool": "x"}})
    assert transport.calls[0][1:3] == ("x", {"idempotency_key": "stage_1"})


def test_a_long_job_is_polled_with_progress_events(
    connectors: Connectors, transport: FakeTransport, db: Path
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "status"}]
    transport.outputs["status"] = [
        {"status": "running", "progress": 0.4, "message": "frames"},
        {"status": "running", "progress": "n/a"},
        {"status": "done", "url": "https://cdn/clip.mp4"},
    ]
    call = connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert [c[1:3] for c in transport.calls[1:]] == [("status", {"job_id": "j1"})] * 3
    assert (call.status, call.polls, call.progress) == ("done", 3, 1.0)
    assert call.response == {"status": "done", "url": "https://cdn/clip.mp4"}
    seen = [(e["status"], e["progress"], e["message"]) for e in events(db)]
    assert seen == [
        ("running", None, None),
        ("polling", 0.0, None),
        ("polling", 0.4, "frames"),
        ("polling", 0.4, "frames"),
        ("done", 1.0, "frames"),
    ]
    assert all(e["run_id"] == "run_1" for e in events(db))


class Crash(BaseException):
    """Stands in for the process dying: nothing records it."""


def test_a_crash_mid_job_resumes_polling_without_calling_again(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "status"}]
    transport.outputs["status"] = [Crash(), {"status": "done"}]
    with pytest.raises(Crash):
        connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert connectors.call_for("stage_1").status == "polling"  # type: ignore[union-attr]
    call = connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert call.status == "done" and call.attempts == 1
    assert [c[1] for c in transport.calls] == ["fal_video", "status", "status"]


def test_a_failed_call_is_recorded_and_a_retry_calls_again(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [ConnectorError("quota exceeded"), {"ok": 1}]
    with pytest.raises(ConnectorError, match="quota"):
        connectors.run_stage(W, "run_1", "stage_1", STAGE)
    failed = connectors.call_for("stage_1")
    assert failed is not None and (failed.status, failed.error) == ("failed", "quota exceeded")
    retried = connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert (retried.status, retried.attempts, retried.error) == ("done", 2, None)
    assert retried.started_at == failed.started_at


def test_a_failed_job_fails_the_stage(connectors: Connectors, transport: FakeTransport) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "status"}]
    transport.outputs["status"] = [{"status": "failed", "error": "out of credits"}]
    with pytest.raises(ConnectorError, match="out of credits"):
        connectors.run_stage(W, "run_1", "stage_1", STAGE)
    failed = connectors.call_for("stage_1")
    assert failed is not None and failed.status == "failed" and failed.polls == 1
    # A failed job is not resumed: the retry starts a fresh call.
    transport.outputs["fal_video"] = [{"done": True}]
    assert connectors.run_stage(W, "run_1", "stage_1", STAGE).status == "done"


def test_a_poll_that_errors_fails_the_stage(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    transport.outputs["fal_video"] = [{"job_id": "j1", "poll": "status"}]
    transport.outputs["status"] = [ConnectorError("gone")]
    with pytest.raises(ConnectorError, match="gone"):
        connectors.run_stage(W, "run_1", "stage_1", STAGE)
    assert connectors.call_for("stage_1").error == "gone"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("calls", "problem"),
    [
        ("nope", "no connector named 'nope'"),
        ("web_search", "no connector named 'web_search'"),
        ("fal_video", "needs its Setup on the Connectors page: fal_key"),
    ],
)
def test_a_stage_that_cannot_call_says_why(
    connectors: Connectors, transport: FakeTransport, calls: str, problem: str
) -> None:
    connectors.add(fal(connectors), {})
    with pytest.raises(ConnectorError, match=problem):
        connectors.run_stage(W, "run_1", "stage_1", {"calls": calls})
    assert transport.calls == []


def test_deleting_the_world_takes_its_connectors_and_calls(
    connectors: Connectors, transport: FakeTransport
) -> None:
    connectors.add(fal(connectors), {"FAL_KEY": "k"})
    connectors.run_stage(W, "run_1", "stage_1", STAGE)
    connectors.delete_world(W)
    assert connectors.find(W, "fal_video") is None and connectors.call_for("stage_1") is None


def test_a_v1_row_that_cannot_become_a_package_is_kept_with_its_key(
    connectors: Connectors, db: Path
) -> None:
    connectors.credentials.set({"_ODD": "k", "FAL_KEY": "f", "YOUTUBE_CLIENT_ID": "yt"})
    with closing(connect(db)) as conn:
        for id_, name, keys in (("c1", "odd", '["_ODD"]'), ("c2", "fal_video", '["FAL_KEY"]')):
            conn.execute(
                "INSERT INTO connectors (id, world_id, name, kind, command, url, credentials,"
                " stage_types, created_at) VALUES (?, ?, ?, 'stage', '[\"uvx\", \"x\"]', NULL,"
                " ?, '[\"connector\"]', '2026-10-01')",
                (id_, W, name, keys),
            )
    connectors.adopt_world_connectors()
    assert [c.name for c in connectors.storage.world_connectors()] == ["odd"]
    assert connectors.enabled(W) == {"fal_video": "0.0.1"}
    assert connectors.credentials.read() == {"_ODD": "k", "YOUTUBE_CLIENT_ID": "yt"}
