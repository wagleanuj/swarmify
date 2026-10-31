"""Worlds enable an installed Connector Package at a pinned version (ADR 0023): its Connector
exists in a world only while that world enables it, called by the package's name."""

import shutil
import sys
from pathlib import Path

import pytest

from swarmify_connectors import Connectors, Credentials, McpTransport, Packages, SqliteStorage
from swarmify_connectors.packages import MANIFEST

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def packages(connectors: Connectors) -> Packages:
    installed = Packages(connectors.packages.root, FIXTURES)
    installed.install("packages/echo")
    return installed


def names(connectors: Connectors, world_id: str) -> list[str]:
    return [c.name for c in connectors.list_all(world_id)]


def test_a_package_exists_only_in_the_worlds_that_enable_it(
    connectors: Connectors, packages: Packages
) -> None:
    assert "echo" not in names(connectors, "w1")
    connectors.enable("w1", "echo", "1.0.0")
    connectors.enable("w2", "echo", "1.0.0")
    echo = connectors.find("w1", "echo")
    assert echo is not None
    assert (echo.world_id, echo.kind, echo.version) == ("w1", "stage", "1.0.0")
    assert echo.command == ["uv", "run", "server.py"]
    assert echo.folder == str(packages.root / "echo/1.0.0")
    assert echo.input_types == ["line"] and "line" in echo.types
    assert names(connectors, "w1")[-1] == "echo" and "echo" not in names(connectors, "w3")

    assert connectors.disable("w1", "echo")
    assert connectors.enabled("w1") == {} and connectors.enabled("w2") == {"echo": "1.0.0"}
    assert connectors.find("w1", "echo") is None and connectors.find("w2", "echo") is not None
    assert not connectors.disable("w1", "echo")

    connectors.delete_world("w2")
    assert connectors.enabled("w2") == {}


def test_enabling_again_moves_the_pin(connectors: Connectors, packages: Packages) -> None:
    source = FIXTURES / "packages/echo"
    newer = connectors.packages.root.parent / "src/echo"
    shutil.copytree(source, newer)
    (newer / MANIFEST).write_text(
        (newer / MANIFEST).read_text().replace('version = "1.0.0"', 'version = "1.1.0"')
    )
    packages.install(str(newer))
    connectors.enable("w1", "echo", "1.0.0")
    connectors.enable("w1", "echo", "1.1.0")
    assert connectors.enabled("w1") == {"echo": "1.1.0"}


@pytest.mark.parametrize(
    ("name", "version", "why"),
    [
        ("echo", "2.0.0", "echo 2.0.0 is not installed"),
        ("nothing", "1.0.0", "nothing 1.0.0 is not installed"),
        ("..", "1.0.0", "is not installed"),
        ("graph", "1.0.0", "Agent Tool"),
    ],
)
def test_only_an_installed_version_under_a_free_name_enables(
    connectors: Connectors, packages: Packages, name: str, version: str, why: str
) -> None:
    with pytest.raises(ValueError, match=why):
        connectors.enable("w1", name, version)
    assert connectors.enabled("w1") == {}


def test_a_world_never_adds_a_connector_named_as_a_package_it_enables(
    connectors: Connectors, packages: Packages
) -> None:
    connectors.enable("w2", "echo", "1.0.0")
    with pytest.raises(ValueError, match="a Connector Package this world enables already"):
        connectors.draft("w2", "echo", "stage", "uvx echo", [], ["connector"])


def test_a_package_whose_version_went_missing_drops_out(
    connectors: Connectors, packages: Packages
) -> None:
    connectors.enable("w1", "echo", "1.0.0")
    shutil.rmtree(packages.root / "echo/1.0.0")
    assert "echo" not in names(connectors, "w1")
    assert connectors.enabled("w1") == {"echo": "1.0.0"}  # the pin stays for the operator


def test_a_stage_calls_an_enabled_package_started_in_its_folder(tmp_path: Path, db: Path) -> None:
    source = tmp_path / "src/talker"
    shutil.copytree(FIXTURES / "packages/echo", source)
    shutil.copy(Path(__file__).parent / "echo_server.py", source / "server.py")
    toml = source / MANIFEST
    command = f'command = ["{sys.executable}", "server.py", "."]'
    toml.write_text(
        "\n".join(
            command
            if line.startswith("command =")
            else 'install = ["python3", "-c", "pass"]'
            if line.startswith("install =")
            else line
            for line in toml.read_text().splitlines()
        )
    )
    Packages(tmp_path / "packages", tmp_path).install(str(source))
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    real.enable("w1", "echo", "1.0.0")
    real.setups.put("echo", "w1", {"api_key": "k"}, {})  # its one required Setup field
    call = real.run_stage("w1", "r1", "s1", {"calls": "echo", "params": {"tool": "echo"}})
    assert call.status == "done" and call.response == {"text": "", "idempotency_key": "s1"}
