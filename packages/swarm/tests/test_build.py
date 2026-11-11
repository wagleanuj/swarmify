"""The build session (ADR 0018) through the real Agent SDK client with a scripted CLI: what the
session is started with (sandbox, path rules, environment, the package's prompts) and what its
hooks refuse."""

import asyncio
from pathlib import Path
from typing import Any, cast

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
from swarmify_swarm.fake import Call, Die, Lost, Say, Script, Step
from swarmify_swarm.network import NetworkWait
from swarmify_swarm.sdk import options as options_for

DOMAINS = ["registry.npmjs.org", "pypi.org"]
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
        network=kw.pop("network", None),
    )
    secrets = ["YOUTUBE_CLIENT_SECRET", "UPLOAD_POST_API_KEY"]
    swarm.build(
        W, R, S, "video", world, folder,
        system="You build pages.", task=f"Build `Main` from {script}.", brief="Make it loud.",
        sandbox=build.sandbox(folder, world, data, data / "npm-cache", secrets, DOMAINS),
        env={**dict.fromkeys(secrets, ""), "npm_config_cache": str(data / "npm-cache")},
        data_dir=data, **kw,
    )  # fmt: skip


def _scripted(board: SqliteStorage, data: Path, *sessions: list[Step], **kw: Any) -> Script:
    script = Script(*sessions)
    _build(board, data, SdkRuntime(transport=script.transport), **kw)
    return script


def test_nothing_readable_holds_the_build_folder(tmp_path: Path) -> None:
    """Under bubblewrap a read-only path over the writable build folder leaves the build folder
    read-only and the sandbox cannot start (every command failed on Linux), so the session reads
    the world's assets and the run's other stage folders, never the world, run or stage folder;
    and the build folder itself, which Seatbelt no longer reads from its write (issue 332)."""
    world = tmp_path / "worlds/w"
    folder = world / "runs/r/build-stage/build"
    for d in (folder, world / "assets/voices", world / "runs/r/script-stage/artifacts",
              world / "runs/r/gate-stage", world / "runs/other-run/x"):  # fmt: skip
        d.mkdir(parents=True)
    rules = build.sandbox(folder, world, tmp_path, tmp_path / "npm-cache", [])["filesystem"]
    read = [Path(p) for p in rules["allowRead"]]
    assert not any(folder.resolve().is_relative_to(p) for p in read if p != folder.resolve())
    assert set(read) == {
        folder.resolve(),
        (tmp_path / "npm-cache").resolve(),
        (world / "assets").resolve(),
        (world / "runs/r/gate-stage").resolve(),
        (world / "runs/r/script-stage").resolve(),
    }
    assert rules["allowWrite"][0] == str(folder.resolve())


def test_the_session_is_sandboxed_in_its_build_folder_without_keys(
    board: SqliteStorage, data: Path
) -> None:
    world, folder, script = _folders(data)
    sdk = _scripted(board, data, [Say("A loud title card.")], model="claude-sonnet-5-5")
    (options,) = sdk.sessions
    assert options.cwd == folder and options.model == "claude-sonnet-5-5"
    assert options.system_prompt == "You build pages."
    assert options.plugins == []  # Claude Code refuses writes inside a plugin's folder
    assert set(options.allowed_tools) == {*build.TOOLS, "StructuredOutput", "ToolSearch"}
    assert options.sandbox == {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": DOMAINS},  # the package's permissions.network
        "filesystem": {
            "allowWrite": [str(folder), str(data / "npm-cache")],
            "denyRead": [str(data)],
            "allowRead": [str(folder), str(data / "npm-cache"), str(world / "runs" / R / "s0")],
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


def test_the_stages_other_inputs_are_named_in_the_prompt(board: SqliteStorage, data: Path) -> None:
    timings = data / "worlds" / W / "runs" / R / "s1" / "artifacts" / "timings.json"
    sdk = _scripted(board, data, [Say("Done.")], inputs=[timings])
    assert str(timings) in sdk.prompts[0]


def test_the_world_assets_are_named_in_the_prompt_as_read_only(
    board: SqliteStorage, data: Path
) -> None:
    assert "read-only" not in _scripted(board, data, [Say("Done.")]).prompts[0]
    bed = data / "worlds" / W / "assets" / "bed-calm.mp3"
    bed.parent.mkdir()
    bed.write_bytes(b"ID3")
    sdk = _scripted(board, data, [Say("Done.")])
    assert str(bed) in sdk.prompts[0] and "read-only" in sdk.prompts[0]


def test_the_world_assets_are_readable_and_never_writable(tmp_path: Path) -> None:
    world = tmp_path / "worlds/w"
    folder, bed = world / "runs/r/s/build", world / "assets/bed.mp3"
    folder.mkdir(parents=True)
    bed.parent.mkdir()
    bed.write_bytes(b"ID3")
    for tool, input in [
        ("Read", {"file_path": str(bed)}),
        ("Glob", {"pattern": "*.mp3", "path": str(bed.parent)}),
        ("Grep", {"pattern": "x", "path": str(bed.parent)}),
        ("Bash", {"command": f"cp {bed.resolve()} public/bed.mp3"}),
    ]:
        assert build.refusal(tool, input, folder, world, tmp_path) is None, tool
    for tool in ("Write", "Edit"):
        got = build.refusal(tool, {"file_path": str(bed)}, folder, world, tmp_path)
        assert got == f"{tool} is limited to the build folder {folder.resolve()}."
    rules = build.sandbox(folder, world, tmp_path, tmp_path / "npm-cache", [])["filesystem"]
    assert any(bed.resolve().is_relative_to(p) for p in rules["allowRead"])
    assert not any(bed.resolve().is_relative_to(p) for p in rules["allowWrite"])


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


def test_a_build_without_minutes_has_no_time_limit(board: SqliteStorage, data: Path) -> None:
    class Patient:
        async def run(self, spec: SessionSpec, hooks: SessionHooks) -> SessionEnd:
            await asyncio.sleep(0.2)
            return SessionEnd("s", 0.02, False, "Built it.")

    assert build.MINUTES is None
    _build(board, data, Patient())  # with minutes=0.001 this same session runs out of time
    assert "Out of time" not in " ".join(e.detail for e in board.list_log(S))


def test_a_spent_budget_stops_the_build_before_it_starts(board: SqliteStorage, data: Path) -> None:
    script = Script([Say("never")])
    with pytest.raises(Stopped, match="spent"):
        _build(board, data, SdkRuntime(transport=script.transport), budget=lambda _: "spent")
    assert script.sessions == []


def test_only_a_sandboxed_session_runs_on_the_cli_that_can_start_its_sandbox(
    board: SqliteStorage, data: Path
) -> None:
    """In Docker the api container cannot start an OS sandbox, so a Build Session's CLI runs
    in the sandbox runner (`sandboxed_cli`); any other session keeps the bundled CLI."""
    script = Script([Say("Done.")])
    runner = "/app/docker/claude-in-runner"
    _build(board, data, SdkRuntime(transport=script.transport, sandboxed_cli=runner))
    (options,) = script.sessions
    assert options.cli_path == runner
    plain = SessionSpec("system", "prompt", data, None, [], [])
    assert options_for(plain, cast(Any, None), None, runner).cli_path is None


def test_a_session_takes_messages_past_the_sdks_1_mb_default(data: Path) -> None:
    """A builder reading a few full-size frame previews sends the CLI a tool result of several
    MB of base64; at the SDK's 1 MB default the session died (run_01M6AV36W27XBFFJWRS4MG1AEG)."""
    plain = SessionSpec("system", "prompt", data, None, [], [])
    size = options_for(plain, cast(Any, None)).max_buffer_size
    assert size is not None and size >= 32 * 1024 * 1024


def test_a_shared_skills_files_are_readable_and_a_link_elsewhere_is_not(tmp_path: Path) -> None:
    world = tmp_path / "worlds/w"
    folder = world / "runs/r/s/build"
    folder.mkdir(parents=True)
    ref = tmp_path / "skills/verify/references/notes.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("x")
    (world / "skills").mkdir()
    (world / "skills/verify").symlink_to(ref.parents[1], target_is_directory=True)
    (world / "skills/out").symlink_to(tmp_path, target_is_directory=True)
    linked = world / "skills/verify/references/notes.md"
    assert build.refusal("Read", {"file_path": str(linked)}, folder, world, tmp_path) is None
    assert (
        build.refusal("Grep", {"pattern": "x", "path": str(ref.parent)}, folder, world, tmp_path)
        is None
    )
    out = {"file_path": str(world / "skills/out/worlds/other.md")}
    assert build.refusal("Read", out, folder, world, tmp_path) is not None
    assert build.refusal("Write", {"file_path": str(linked)}, folder, world, tmp_path) is not None


def test_a_big_asset_folder_is_one_line_in_the_prompt(board: SqliteStorage, data: Path) -> None:
    assets = data / "worlds" / W / "assets"
    (assets / "library").mkdir(parents=True)
    for i in range(500):
        (assets / "library" / f"clip{i}.wav").write_bytes(b"RIFF")
    (assets / "sfx").mkdir()
    (assets / "sfx/whoosh.wav").write_bytes(b"RIFF")
    (assets / "bed.mp3").write_bytes(b"ID3")
    sent = _scripted(board, data, [Say("Done.")]).prompts[0]
    assert f"{assets / 'library'}/ (500 files)" in sent and "clip0.wav" not in sent
    assert str(assets / "sfx/whoosh.wav") in sent and str(assets / "bed.mp3") in sent


def _paused(board: SqliteStorage, data: Path) -> tuple[Script, Path]:
    """A build the run's pause stops part way, after its first tool call wrote a file. Returns
    its script and the file."""
    _, folder, _ = _folders(data)
    checks: list[None] = []

    def budget(_: str) -> str | None:  # the start's and the first step's checks pass
        checks.append(None)
        if len(checks) <= 2:
            return None
        (folder / "half.txt").write_text("half")
        return "The run was paused."

    steps = [Call("Bash", {"command": "ls"}), Call("Bash", {"command": "pwd"}), Say("never")]
    first = Script(steps, usage={"input_tokens": 1000})
    with pytest.raises(Stopped, match="paused"):
        _build(board, data, SdkRuntime(transport=first.transport), budget=budget)
    return first, folder / "half.txt"


def test_a_stopped_build_resumes_its_session_told_to_continue(
    board: SqliteStorage, data: Path
) -> None:
    """Issue 423: re-entered for the same visit (a pause, a restart), the builder continues its
    stored session, with a short prompt instead of the brief, its folder as it left it."""
    first, half = _paused(board, data)
    [stored] = {a.session_id for a in board.list_agents(R)}
    assert stored and "Make it loud." in first.prompts[0]

    again = _scripted(board, data, [Say("Finished.")], resume=True)
    (options,) = again.sessions
    assert options.resume == stored and again.prompts == [build.CONTINUE]
    assert half.read_text() == "half"
    assert f"Continuing session {stored}." in [e.detail for e in board.list_log(S)]


def test_a_resumed_build_whose_transcript_is_gone_starts_over_with_its_progress(
    board: SqliteStorage, data: Path
) -> None:
    _paused(board, data)
    again = _scripted(board, data, [Lost()], [Say("Finished.")], resume=True)
    assert again.sessions[0].resume and again.sessions[1].resume is None
    assert "Make it loud." in again.prompts[1]
    assert "Your earlier session in this stage was lost." in again.prompts[1]
    assert '- Bash {"command": "ls"}' in again.prompts[1]  # its progress, from the stage log


def test_a_build_not_resumed_starts_a_new_session_and_a_dead_one_is_continued(
    board: SqliteStorage, data: Path
) -> None:
    """A new visit starts a new session with the brief, though one is stored; a replacement for a
    session that died continues it, told to go on."""
    _paused(board, data)
    again = _scripted(board, data, [Die()], [Say("Finished.")])
    assert again.sessions[0].resume is None and "Make it loud." in again.prompts[0]
    assert again.sessions[1].resume and again.prompts[1] == build.CONTINUE


def test_a_build_that_loses_the_network_waits_and_continues_its_session(
    board: SqliteStorage, data: Path
) -> None:
    """The blender_build of run run_01M75KPE82TM2FCBCT3DVJBRD6 died twice with EAI_AGAIN while
    the Wi-Fi was down and the builder gave up: now it waits and continues instead."""
    offline = Die("API Error: Can't reach the API server (EAI_AGAIN)")
    probes = iter([False, False, True, True, True])
    wait = NetworkWait(probe=lambda: next(probes), sleep=lambda _: None)
    script = _scripted(
        board, data, [Call("Bash", {"command": "ls"}), offline], [offline], [offline],
        [Say("Finished.")], network=wait,
    )  # fmt: skip
    [agent] = board.list_agents(R)
    first, *again = script.sessions
    assert agent.status == "done" and first.resume is None and len(again) == 3
    assert all(s.resume == agent.session_id for s in again)
    assert script.prompts[1:] == [build.CONTINUE] * 3
