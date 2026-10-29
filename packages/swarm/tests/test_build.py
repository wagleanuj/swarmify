"""The build session (ADR 0018) through the real Agent SDK client with a scripted CLI: what the
session is started with (sandbox, path rules, environment) and what its hooks refuse."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from swarm_db import DiskFiles
from swarmify_swarm import (
    SdkRuntime,
    SessionEnd,
    SessionHooks,
    SessionSpec,
    SqliteStorage,
    StageError,
    Stopped,
    Swarm,
    build,
)
from swarmify_swarm.fake import Call, Say, Script, Step

W, R, S = "world_1", "run_1", "stage_1"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A data directory with keys, two worlds and this stage's build folder."""
    (tmp_path / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cret\n")
    (tmp_path / "worlds" / "other").mkdir(parents=True)
    (tmp_path / "worlds" / W / "runs" / R / S / "build").mkdir(parents=True)
    script = tmp_path / "worlds" / W / "runs" / R / "s0" / "artifacts" / "script.md"
    script.parent.mkdir(parents=True)
    script.write_text("# Hook\n")
    return tmp_path


def _folders(data: Path) -> tuple[Path, Path, Path]:
    world = data / "worlds" / W
    return world, world / "runs" / R / S / "build", world / "runs" / R / "s0/artifacts/script.md"


def _build(board: SqliteStorage, data: Path, runtime: Any, **kw: Any) -> None:
    world, folder, script = _folders(data)
    swarm = Swarm(
        board,
        runtime,
        DiskFiles(data / "worlds"),
        budget=kw.pop("budget", None) or (lambda _: None),
    )
    secrets = ["YOUTUBE_CLIENT_SECRET", "UPLOAD_POST_API_KEY"]
    swarm.build(
        W, R, S, "video", world, folder, script,
        composition="Main", brief="Make it loud.",
        sandbox=build.sandbox(folder, world, data, data / "npm-cache", secrets),
        env={**dict.fromkeys(secrets, ""), "npm_config_cache": str(data / "npm-cache")},
        data_dir=data, **kw,
    )  # fmt: skip


def _scripted(board: SqliteStorage, data: Path, *sessions: list[Step], **kw: Any) -> Script:
    script = Script(*sessions)
    _build(board, data, SdkRuntime(transport=script.transport), **kw)
    return script


def test_the_session_is_sandboxed_in_its_build_folder_without_keys(
    board: SqliteStorage, data: Path
) -> None:
    world, folder, script = _folders(data)
    sdk = _scripted(board, data, [Say("A loud title card.")], model="claude-sonnet-5-5")
    (options,) = sdk.sessions
    assert options.cwd == folder and options.model == "claude-sonnet-5-5"
    assert options.plugins == []  # Claude Code refuses writes inside a plugin's folder
    assert set(options.allowed_tools) == {*build.TOOLS, "StructuredOutput", "ToolSearch"}
    assert options.sandbox == {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {
            "allowedDomains": ["registry.npmjs.org", "fonts.googleapis.com", "fonts.gstatic.com"]
        },
        "filesystem": {
            "allowWrite": [str(folder), str(data / "npm-cache")],
            "denyRead": [str(data)],
            "allowRead": [str(folder), str(data / "npm-cache"), str(world)],
        },
        "credentials": {
            "envVars": [
                {"name": n, "mode": "deny"}
                for n in [
                    "YOUTUBE_CLIENT_SECRET",
                    "UPLOAD_POST_API_KEY",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_API_KEY",
                ]
            ]
        },
    }
    # keys are blanked over whatever the API process inherited
    assert options.env["YOUTUBE_CLIENT_SECRET"] == "" and options.env["UPLOAD_POST_API_KEY"] == ""
    assert options.env["npm_config_cache"] == str(data / "npm-cache")
    assert str(script) in sdk.prompts[0] and "`Main`" in sdk.prompts[0]
    assert "Make it loud." in sdk.prompts[0]
    [agent] = board.list_agents(R)
    assert (agent.name, agent.status, agent.cost_usd) == ("builder", "done", 0.01)


def test_the_run_decisions_are_required_reading(board: SqliteStorage, data: Path) -> None:
    decision = data / "worlds" / W / "runs" / R / "s1" / "decision.md"
    decision.parent.mkdir(parents=True)
    decision.write_text(
        '---\nstage: "pick"\ncreated: "2026-10-28T00:00:00"\n'
        'artifact: "runs/run_1/s0/artifacts/ideas.md"\n---\n\n# Decision\n\nPicked idea 2.\n'
    )
    sdk = _scripted(board, data, [Say("Done.")])
    assert "Decision at gate pick" in sdk.prompts[0] and "Picked idea 2." in sdk.prompts[0]


def test_the_hooks_keep_every_file_tool_in_the_folder(board: SqliteStorage, data: Path) -> None:
    world, folder, script = _folders(data)
    (folder / "out").symlink_to(data)  # a link the agent made, pointing out
    calls = [
        Call("Write", {"file_path": str(folder / "src/Main.tsx"), "content": "x"}),
        Call("Edit", {"file_path": str(folder / "package.json")}),
        Call("Read", {"file_path": str(script)}),
        Call("Glob", {"pattern": "**/*.tsx"}),
        Call("Bash", {"command": f"cd {folder} && npm install"}),
        Call("Write", {"file_path": str(data / "evil.txt"), "content": "x"}),
        Call("Write", {"file_path": str(folder / "../../escaped.txt"), "content": "x"}),
        Call("Edit", {"file_path": str(folder / "out/.env")}),
        Call("Read", {"file_path": str(data / ".env")}),
        Call("Read", {"file_path": str(folder / "out/.env")}),
        Call("Read", {"file_path": str(data / "worlds/other/notes.md")}),
        Call("Grep", {"pattern": "KEY", "path": str(data)}),
        Call("Glob", {"pattern": "/etc/*"}),
        Call("Bash", {"command": "curl evil.example", "dangerouslyDisableSandbox": True}),
        Call("Bash", {"command": f"cat {data}/.env"}),
        Call("Bash", {"command": "cat ../../../../../.env"}),
        Call("WebFetch", {"url": "https://evil.example"}),
        Say("Done."),
    ]
    sdk = _scripted(board, data, calls)
    allowed = [o.is_error for o in sdk.outcomes]
    assert allowed == [False] * 5 + [True] * 12, sdk.outcomes
    refusals = {o.text for o in sdk.outcomes if o.is_error}
    assert f"Write is limited to the build folder {folder.resolve()}." in refusals
    assert "Bash runs only inside the sandbox." in refusals
    assert "WebFetch is not allowed in this stage." in refusals
    assert not (data / "evil.txt").exists()
    denied = [e for e in board.list_log(S) if e.kind == "denied"]
    assert len(denied) == 12 and denied[0].agent == "builder"


@pytest.mark.parametrize(
    ("tool", "input", "refused"),
    [
        ("Write", {"file_path": "src/x.tsx"}, False),  # relative to the build folder
        ("Write", {}, True),
        ("Read", {"file_path": "~/.ssh/id_rsa"}, True),
        ("Grep", {"pattern": "x"}, False),  # no path: the working directory
        ("Read", {"file_path": "public/.env"}, True),
        ("Bash", {"command": "ls ~/.swarmify-elsewhere"}, False),
        ("Bash", {"command": "npx tsc --noEmit"}, False),
        ("Bash", {"command": "cp .env.example x"}, False),
        ("Bash", {"command": "source .env"}, True),
        ("Bash", {"command": "cat {world}/runs/r/s0/artifacts/script.md"}, False),
        ("Bash", {"command": "cat {data}/worlds/x/notes.md"}, True),
    ],
)
def test_the_path_rules(tmp_path: Path, tool: str, input: dict, refused: bool) -> None:
    folder = tmp_path / "worlds/w/runs/r/s/build"
    folder.mkdir(parents=True)
    if "command" in input:
        world = str((tmp_path / "worlds/w").resolve())
        input = {"command": input["command"].format(world=world, data=tmp_path.resolve())}
    got = build.refusal(tool, input, folder, tmp_path / "worlds/w", tmp_path)
    assert (got is not None) == refused, got


def test_two_failed_sessions_fail_the_stage_with_the_last_log_lines(
    board: SqliteStorage, data: Path
) -> None:
    class Failing:
        async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
            hooks.before_tool("Bash", {"command": "npx tsc --noEmit"})
            return SessionEnd("s", 0.02, True, "tsc found 3 errors")

    with pytest.raises(StageError) as raised:
        _build(board, data, Failing())
    assert "builder failed 2 sessions in a row" in str(raised.value)
    assert "The session failed: tsc found 3 errors" in str(raised.value)
    assert "tool: Bash" in str(raised.value)
    [agent] = board.list_agents(R)
    assert agent.cost_usd == 0  # Failing reports no cost increments
    assert agent.status == "failed"


def test_the_time_limit_fails_the_stage(board: SqliteStorage, data: Path) -> None:
    class Slow:
        async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
            await asyncio.sleep(5)
            raise AssertionError("not cancelled")

    with pytest.raises(StageError, match=r"ran out of time \(0.001 minutes\)"):
        _build(board, data, Slow(), minutes=0.001)
    assert "Out of time" in board.list_log(S)[-1].detail


def test_a_spent_budget_stops_the_build_before_it_starts(board: SqliteStorage, data: Path) -> None:
    script = Script([Say("never")])
    with pytest.raises(Stopped, match="spent"):
        _build(board, data, SdkRuntime(transport=script.transport), budget=lambda _: "spent")
    assert script.sessions == []
