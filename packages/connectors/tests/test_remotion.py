"""What the core keeps of Remotion for `remotion_build` until it is a package (issue 217): the
build folder's seed and its stage's check. The render is the remotion_render package's, tested
beside it in `connectors/remotion_render`."""

from pathlib import Path

import pytest

from swarmify_connectors import Connectors, remotion

W = "world_1"


def test_a_build_is_seeded_from_the_starter_or_a_template_by_name(tmp_path: Path) -> None:
    folder = tmp_path / "worlds/w/runs/r/s/build"
    remotion.seed(folder, None, tmp_path)
    assert "registerRoot" in (folder / "src/index.ts").read_text()
    assert 'id="Main"' in (folder / "src/Root.tsx").read_text()
    assert (folder / "package-lock.json").is_file()

    shorts = tmp_path / "remotion/shorts"
    (shorts / "node_modules/x").mkdir(parents=True)
    (shorts / "src").mkdir()
    (shorts / "src/index.ts").write_text("// shorts")
    remotion.seed(folder, "shorts", tmp_path)  # a fresh folder each time
    assert (folder / "src/index.ts").read_text() == "// shorts"
    assert not (folder / "node_modules").exists() and not (folder / "src/Root.tsx").exists()
    for base in ("../shorts", str(shorts), "nope"):
        with pytest.raises(ValueError):
            remotion.seed(folder, base, tmp_path)


def test_a_saved_build_stage_names_its_script_and_an_existing_template(
    tmp_path: Path, connectors: Connectors
) -> None:
    build = {"calls": "remotion_build", "output": "video.mp4"}
    assert connectors.stage_problems(W, build) == ["remotion_build needs an input, the script"]
    assert connectors.stage_problems(W, {**build, "inputs": ["script.md", "t.json"]}) == []
    build["input"] = "script.md"
    assert connectors.stage_problems(W, build) == []
    assert connectors.stage_problems(W, {**build, "params": {"base": "/etc"}}) == [
        f"base must be a template name under {tmp_path / 'remotion'}, not a path"
    ]
    assert connectors.stage_problems(W, {**build, "params": {"base": "shorts"}}) == [
        f"the Remotion template shorts does not exist in {tmp_path / 'remotion'}"
    ]


def test_secret_names_cover_the_file_and_every_declared_key(
    tmp_path: Path, connectors: Connectors
) -> None:
    (tmp_path / ".env").write_text("OTHER_KEY=x\n")
    (tmp_path / ".env").chmod(0o600)
    names = connectors.secret_names(W)
    assert "OTHER_KEY" in names
