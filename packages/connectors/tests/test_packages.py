"""Connector Packages: Manifests checked against fixture files, one per rule, and installs of a
fixture package from a folder into a packages folder."""

import json
import shutil
from pathlib import Path

import pytest

from swarmify_connectors import InstallError, ManifestError, Packages, read_manifest
from swarmify_connectors.packages import MANIFEST, RECORD

FIXTURES = Path(__file__).parent / "fixtures"
INVALID = sorted((FIXTURES / "manifests").glob("*.toml"))


def test_the_fixture_package_declares_every_section_and_passes() -> None:
    manifest = read_manifest(FIXTURES / "packages/echo")
    assert manifest["name"] == "echo"
    assert [c["key"] for c in manifest["config"]] == ["api_key", "endpoint", "voice", "account"]
    assert manifest["services"][0]["url"] == "endpoint"


@pytest.mark.parametrize("fixture", INVALID, ids=[f.stem for f in INVALID])
def test_an_invalid_manifest_is_refused_naming_the_field(fixture: Path, tmp_path: Path) -> None:
    expected = fixture.read_text().splitlines()[0].removeprefix("# error: ")
    shutil.copy(fixture, tmp_path / MANIFEST)
    with pytest.raises(ManifestError) as raised:
        read_manifest(tmp_path)
    assert str(raised.value).startswith(expected)


def test_a_folder_without_a_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="has no swarmify-connector.toml"):
        read_manifest(tmp_path)


@pytest.fixture
def packages(tmp_path: Path) -> Packages:
    return Packages(tmp_path / "data/packages", FIXTURES)


def copy_echo(tmp_path: Path, install: str) -> Path:
    folder = tmp_path / "src/echo"
    shutil.copytree(FIXTURES / "packages/echo", folder)
    toml = folder / MANIFEST
    toml.write_text(
        "\n".join(
            f"install = {install}" if line.startswith("install =") else line
            for line in toml.read_text().splitlines()
        )
    )
    return folder


def test_installing_from_a_repo_folder_copies_it_by_name_and_version_and_runs_its_step(
    packages: Packages,
) -> None:
    package = packages.install("packages/echo")
    folder = packages.root / "echo/1.0.0"
    assert (folder / MANIFEST).is_file() and (folder / "server.py").is_file()
    assert (folder / "installed.txt").read_text() == "ok"  # the step ran in the copy
    assert not (FIXTURES / "packages/echo/installed.txt").exists()
    assert (package.name, package.version, package.label, package.kind) == (
        "echo",
        "1.0.0",
        "Echo",
        "stage",
    )
    assert (package.state, package.source, package.problem) == ("installed", "packages/echo", None)
    assert packages.list() == [package]


def test_a_version_installs_once(packages: Packages) -> None:
    packages.install("packages/echo")
    with pytest.raises(InstallError, match="echo 1.0.0 is already installed"):
        packages.install("packages/echo")


def test_a_failed_install_step_leaves_nothing_behind(packages: Packages, tmp_path: Path) -> None:
    source = copy_echo(tmp_path, '["python3", "-c", "import sys; sys.exit(\'no network\')"]')
    with pytest.raises(InstallError, match=r"install step .* failed \(1\): no network"):
        packages.install(str(source))
    assert not (packages.root / "echo").exists()
    assert packages.list() == []


def test_an_install_step_runs_in_the_sandbox_and_its_error_says_what_it_blocked(
    packages: Packages, tmp_path: Path
) -> None:
    """An install step writes only its version folder: reading the data directory's Setup
    values is refused, and the error the Connectors page shows says so."""
    secrets = tmp_path / "data" / "setup.json"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text('{"other": {"shared": {"api_key": "s3cret"}}}')
    step = json.dumps(
        ["python3", "-c", "import sys; print(open(sys.argv[1]).read())", str(secrets)]
    )
    with pytest.raises(InstallError) as failed:
        packages.install(str(copy_echo(tmp_path, step)))
    assert "s3cret" not in str(failed.value)
    assert f"The sandbox blocked: read {secrets.resolve()}" in str(failed.value)


def test_an_install_step_that_cannot_start_leaves_nothing_behind(
    packages: Packages, tmp_path: Path
) -> None:
    source = copy_echo(tmp_path, '["no-such-tool-anywhere"]')
    # it starts in the sandbox, where the shell finds no such command
    with pytest.raises(InstallError, match=r"failed \(127\): .*no-such-tool-anywhere"):
        packages.install(str(source))
    assert not (packages.root / "echo").exists()


def test_an_invalid_manifest_installs_nothing(packages: Packages, tmp_path: Path) -> None:
    source = tmp_path / "src/bad"
    source.mkdir(parents=True)
    shutil.copy(FIXTURES / "manifests/bad_kind.toml", source / MANIFEST)
    with pytest.raises(ManifestError, match="^kind: "):
        packages.install(str(source))
    assert not packages.root.exists()


def test_a_local_path_installs_and_a_half_install_is_neither_listed_nor_in_the_way(
    packages: Packages, tmp_path: Path
) -> None:
    leftover = packages.root / "echo/1.0.0"
    leftover.mkdir(parents=True)  # a crash mid-install: no record
    assert packages.list() == []
    source = copy_echo(tmp_path, '["python3", "-c", "pass"]')
    assert packages.install(str(source)).source == str(source)
    assert [p.version for p in packages.list()] == ["1.0.0"]


def test_a_source_outside_the_repo_or_missing_is_refused(packages: Packages) -> None:
    with pytest.raises(InstallError, match="outside this repository"):
        packages.install("../..")
    with pytest.raises(InstallError, match="no package folder"):
        packages.install("packages/nothing")


def test_an_installed_package_whose_manifest_went_bad_lists_as_invalid(
    packages: Packages,
) -> None:
    packages.install("packages/echo")
    toml = packages.root / "echo/1.0.0" / MANIFEST
    toml.write_text(toml.read_text().replace('kind = "stage"', 'kind = "robot"'))
    [package] = packages.list()
    assert package.state == "invalid"
    assert package.problem is not None and package.problem.startswith("kind: ")
    assert (packages.root / "echo/1.0.0" / RECORD).is_file()
