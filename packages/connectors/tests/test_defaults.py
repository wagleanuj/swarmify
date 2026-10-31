"""Default packages (ADR 0023, issue 213): this repository's `connectors/` packages install at
startup when missing, carry what V1 kept for them into their shared Setup, and are given to each
world once; the world's own choice stands after that."""

import shutil
from pathlib import Path

import pytest

from swarmify_connectors import Connectors
from swarmify_connectors.packages import MANIFEST

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path: Path, connectors: Connectors) -> Path:
    """A repository whose `connectors/` holds echo, probe and one with a broken Manifest."""
    repo = tmp_path / "repo"
    for name in ("echo", "probe"):
        shutil.copytree(FIXTURES / "packages" / name, repo / "connectors" / name)
    (repo / "connectors" / "broken").mkdir()
    (repo / "connectors" / "broken" / MANIFEST).write_text('name = "broken"\n')
    connectors.packages.repo = repo
    return repo


def test_defaults_install_once_and_a_broken_one_is_left_out(
    connectors: Connectors, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert connectors.install_defaults() == {"echo": "1.0.0", "probe": "1.0.0"}
    assert "connectors/broken did not install" in caplog.text
    installed = connectors.packages.root / "echo" / "1.0.0"
    assert (installed / "installed.txt").is_file()  # its install step ran
    (installed / "installed.txt").unlink()
    assert connectors.install_defaults() == {"echo": "1.0.0", "probe": "1.0.0"}
    assert not (installed / "installed.txt").exists()  # installed already: not again


def test_the_first_install_carries_v1_s_settings_and_keys_into_the_shared_setup(
    connectors: Connectors, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connectors.credentials.set({"PROBE_KEY": "from-v1", "ECHO_KEY": "echo-v1"})
    monkeypatch.setenv("ENDPOINT", "http://engine:8099")  # a Setting from the app's env
    monkeypatch.setenv("RETRIES", "lots")  # not a whole number: left at its default
    monkeypatch.setenv("VOICE", "high")  # set per world: never shared
    connectors.install_defaults()
    shared, _ = connectors.setups.values("probe", "")
    assert shared == {"api_key": "from-v1", "endpoint": "http://engine:8099"}
    assert connectors.setups.values("echo", "")[0] == {
        "api_key": "echo-v1",
        "endpoint": "http://engine:8099",
    }

    # Later the operator changes it; a new version of the package carries nothing again.
    connectors.setups.put_value("probe", None, "api_key", "mine")
    newer = repo / "connectors" / "probe" / MANIFEST
    newer.write_text(newer.read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
    assert connectors.install_defaults()["probe"] == "1.1.0"
    assert connectors.setups.values("probe", "")[0]["api_key"] == "mine"


def test_each_world_is_given_the_defaults_once_and_its_choice_stands(
    connectors: Connectors, repo: Path
) -> None:
    connectors.install_defaults()
    connectors.offer_defaults("w1")
    assert connectors.enabled("w1") == {"echo": "1.0.0", "probe": "1.0.0"}
    connectors.disable("w1", "echo")
    connectors.offer_defaults("w1")
    assert connectors.enabled("w1") == {"probe": "1.0.0"}

    # A newer default: worlds keep their pins, a new world is given the newer one.
    newer = repo / "connectors" / "probe" / MANIFEST
    newer.write_text(newer.read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
    connectors.install_defaults()
    connectors.offer_defaults("w1")
    connectors.offer_defaults("w2")
    assert connectors.enabled("w1") == {"probe": "1.0.0"}
    assert connectors.enabled("w2") == {"echo": "1.0.0", "probe": "1.1.0"}

    connectors.delete_world("w1")
    connectors.offer_defaults("w1")  # a world made again under the same id starts afresh
    assert connectors.enabled("w1") == {"echo": "1.0.0", "probe": "1.1.0"}


def test_a_declared_service_is_checked_at_each_url_its_setting_holds(
    connectors: Connectors, repo: Path
) -> None:
    connectors.install_defaults()
    connectors.offer_defaults("w1")
    connectors.save_setup("w1", "echo", {"api_key": "k", "endpoint": "http://127.0.0.1:9/"}, {})
    echo = connectors.find("w1", "echo")
    assert echo is not None and echo.health == "echo_health"
    (down,) = connectors.services(echo)  # nothing listens on discard
    assert down.name == "http://127.0.0.1:9" and not down.ok and down.message
    probe = connectors.find("w1", "probe")
    assert probe is not None and connectors.health(probe) == []
