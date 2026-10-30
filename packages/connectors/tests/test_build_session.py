"""The Build Session Capability's side in the connectors context (ADR 0018): the Manifest's
`[build_session]`, the build folder's seed and revisions, the brief, and the workflow check of a
stage calling a package that requests one. The fixture package `sketch` requests one."""

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import Connectors, ManifestError, Packages, build_session, read_manifest
from swarmify_connectors.packages import MANIFEST, check_manifest

SKETCH = Path(__file__).parent / "fixtures/packages/sketch"
W = "world_1"


def _sketch(tmp_path: Path) -> Path:
    folder = tmp_path / "sketch"
    shutil.copytree(SKETCH, folder)
    return folder


def test_the_brief_fills_the_stage_and_adds_the_note_of_each_input_it_has_one_for(
    tmp_path: Path,
) -> None:
    m = read_manifest(SKETCH)
    copy, palette = tmp_path / "copy.md", tmp_path / "palette.json"
    values = {"tone": "calm", "input": copy, "folder": tmp_path / "build"}
    assert (
        build_session.task(m, SKETCH, values, [copy])
        == f"Write a calm page for the copy at {copy}."
    )
    voiced = build_session.task(m, SKETCH, values, [copy, palette])
    assert voiced.endswith(f"Use the colours in {palette} (palette.json).")
    assert (
        build_session.fill("$folder and $unknown", values) == f"{tmp_path / 'build'} and $unknown"
    )


def test_a_build_is_seeded_from_a_folder_or_a_template_by_name(tmp_path: Path) -> None:
    folder = tmp_path / "worlds/w/runs/r/s/build"
    build_session.seed(folder, SKETCH / "session/starter")
    assert (folder / "index.html").is_file()

    shorts = tmp_path / "remotion/shorts"
    (shorts / "node_modules/x").mkdir(parents=True)
    (shorts / ".git").mkdir()
    (shorts / "index.ts").write_text("// shorts")
    build_session.seed(folder, build_session.template("shorts", tmp_path, "remotion"))
    assert (folder / "index.ts").read_text() == "// shorts"  # a fresh folder each time
    assert not (folder / "node_modules").exists() and not (folder / "index.html").exists()
    assert not (folder / ".git").exists()
    for base in ("../shorts", str(shorts), "..", "nope"):
        with pytest.raises(ValueError):
            build_session.template(base, tmp_path, "remotion")


def test_a_revision_keeps_the_last_build_and_its_file_once(tmp_path: Path) -> None:
    folder, version, video = tmp_path / "build", tmp_path / "build.v1", tmp_path / "video.mp4"
    (folder / "node_modules").mkdir(parents=True)
    (folder / "a.txt").write_text("v1")
    video.write_text("first")
    build_session.keep(folder, version, video)
    assert (folder / "a.txt").read_text() == "v1" and not (folder / "node_modules").exists()
    assert (tmp_path / "build.v1.mp4").read_text() == "first"
    (folder / "a.txt").write_text("half done")  # a retried revision starts from v1 again
    video.write_text("second")
    build_session.keep(folder, version, None)
    assert (folder / "a.txt").read_text() == "v1"
    assert (tmp_path / "build.v1.mp4").read_text() == "first"


def _without(m: dict[str, Any], *path: str) -> dict[str, Any]:
    m = copy.deepcopy(m)
    *parents, key = path
    where = m
    for p in parents:
        where = where[p]
    del where[key]
    return m


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        (("capabilities",), "build_session: is the [build_session]"),
        (("build_session",), "build_session: is the [build_session]"),
        (("command",), "command: give the command"),
        (("build_session", "brief"), "build_session.brief: is required"),
        (("build_session", "tool"), "build_session.tool: is required"),
    ],
)
def test_a_bad_build_session_is_refused_naming_the_field(
    path: tuple[str, ...], problem: str
) -> None:
    with pytest.raises(ManifestError) as raised:
        check_manifest(_without(read_manifest(SKETCH), *path))
    assert str(raised.value).startswith(problem)


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('"session/system.md"', '"../system.md"', "build_session.system: '../system.md' does"),
        ('"session/brief.md"', '"/etc/passwd"', "build_session.brief: '/etc/passwd' does"),
        ('"session/starter"', '"nowhere"', "build_session.starter: nowhere is not in the package"),
        ('"session/palette.md"', '"gone.md"', "build_session.notes.palette.json: gone.md is not"),
    ],
)
def test_a_build_session_file_lies_inside_the_package(
    tmp_path: Path, old: str, new: str, problem: str
) -> None:
    folder = _sketch(tmp_path)
    (folder / MANIFEST).write_text((folder / MANIFEST).read_text().replace(old, new))
    with pytest.raises(ManifestError, match=problem):
        read_manifest(folder)


def test_a_session_calling_another_packages_tool_needs_no_server() -> None:
    m = _without(read_manifest(SKETCH), "command")
    m["build_session"]["connector"] = "other"
    assert check_manifest(m)["build_session"]["connector"] == "other"


@pytest.fixture
def sketched(tmp_path: Path, connectors: Connectors) -> Connectors:
    """The fixture package installed and enabled in W, with remotion-style templates."""
    folder = _sketch(tmp_path)
    text = (folder / MANIFEST).read_text()
    text = text.replace('tool = "finish"\n', 'tool = "finish"\ntemplates = "remotion"\n')
    text = text.replace("[build_session]\n", '[build_session]\noutput = "index.html"\n')
    (folder / MANIFEST).write_text(text)
    connectors.packages = Packages(tmp_path / "packages", tmp_path)
    connectors.packages.install(str(folder))
    connectors.enable(W, "sketch", "1.0.0")
    return connectors


def test_a_saved_build_stage_names_its_input_and_an_existing_template(
    tmp_path: Path, sketched: Connectors
) -> None:
    stage: dict[str, Any] = {"calls": "sketch"}  # its output defaults to the Manifest's
    assert sketched.stage_problems(W, stage) == ["sketch needs an input, the copy"]
    stage["inputs"] = ["copy.md", "palette.json"]
    assert sketched.stage_problems(W, stage) == []
    assert sketched.stage_problems(W, {**stage, "params": {"base": "/etc"}}) == [
        f"base must be a template name under {tmp_path / 'remotion'}, not a path"
    ]
    assert sketched.stage_problems(W, {**stage, "params": {"base": "shorts"}}) == [
        f"the template shorts does not exist in {tmp_path / 'remotion'}"
    ]
    (tmp_path / "remotion/shorts").mkdir(parents=True)
    assert sketched.stage_problems(W, {**stage, "params": {"base": "shorts"}}) == []


def test_secret_names_cover_the_file_and_every_declared_key(
    tmp_path: Path, connectors: Connectors
) -> None:
    (tmp_path / ".env").write_text("OTHER_KEY=x\n")
    (tmp_path / ".env").chmod(0o600)
    names = connectors.secret_names(W)
    assert "OTHER_KEY" in names
