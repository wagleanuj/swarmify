"""Setup (ADR 0024): a package's declared fields, their values in the mode-600 setup file, the
test before saving, and each connector process getting exactly its own resolved values."""

import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import (
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
)
from swarmify_connectors.packages import MANIFEST, read_manifest
from swarmify_connectors.setup import coerce, fields

PROBE = Path(__file__).parent / "fixtures/packages/probe"


def install(connectors: Connectors, tmp_path: Path, name: str = "probe", env: str = "") -> None:
    """Install a copy of the probe package as `name`, started with this Python; `env` renames
    its key's variable."""
    source = tmp_path / "src" / name
    shutil.copytree(PROBE, source)
    toml = (source / MANIFEST).read_text()
    toml = toml.replace('name = "probe"', f'name = "{name}"')
    toml = toml.replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
    if env:
        toml = toml.replace('env = "PROBE_KEY"', f'env = "{env}"')
    (source / MANIFEST).write_text(toml)
    connectors.packages.install(str(source))


@pytest.fixture
def probe(connectors: Connectors, tmp_path: Path) -> Connectors:
    install(connectors, tmp_path)
    connectors.enable("w1", "probe", "1.0.0")
    connectors.enable("w2", "probe", "1.0.0")
    return connectors


def field(key: str) -> dict[str, Any]:
    return next(f for f in fields(read_manifest(PROBE)) if f["key"] == key)


@pytest.mark.parametrize(
    ("key", "raw", "value"),
    [
        ("api_key", "abc", "abc"),
        ("notes", "two\nlines", "two\nlines"),
        ("ratio", "0.5", 0.5),
        ("ratio", 1, 1.0),
        ("retries", "4", 4),
        ("loud", "true", True),
        ("loud", False, False),
        ("voice", "high", "high"),
        ("endpoint", "https://probe.example.org/v1", "https://probe.example.org/v1"),
        ("cache", "~/probe-cache", "~/probe-cache"),
        ("cache", "", None),
    ],
)
def test_each_field_type_takes_its_values(key: str, raw: Any, value: Any) -> None:
    assert coerce(field(key), raw) == value


@pytest.mark.parametrize(
    ("key", "raw", "why"),
    [
        ("api_key", "one\ntwo", "API key: is one line"),
        ("ratio", "lots", "Ratio: is a number"),
        ("ratio", "1.5", "Ratio: is at most 1"),
        ("retries", "2.5", "Retries: is a whole number"),
        ("retries", True, "Retries: is a whole number"),
        ("loud", "maybe", "Loud: is yes or no"),
        ("voice", "mid", "Voice: is one of low, high"),
        ("endpoint", "probe.example.org", "Service URL: is an http"),
        ("cache", 5, "Cache folder: is text"),
    ],
)
def test_each_field_type_refuses_what_it_is_not(key: str, raw: Any, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        coerce(field(key), raw)


def test_a_failing_test_saves_nothing_and_a_passing_one_saves(
    probe: Connectors, transport: FakeTransport
) -> None:
    transport.outputs["check_setup"] = [
        {"ok": False, "message": "The probe service refused that key."},
        {"ok": True, "message": "Signed in as probe."},
    ]
    check = probe.save_setup("w1", "probe", {"api_key": "bad", "ratio": "0.2"}, {})
    assert check is not None and (check.ok, check.message) == (
        False,
        "The probe service refused that key.",
    )
    assert probe.setup_values("w1", "probe")[1:] == ({}, {})
    assert transport.calls[-1][1:] == ("check_setup", {}, transport.calls[-1][3])
    assert transport.calls[-1][3]["PROBE_KEY"] == "bad"  # it ran with the candidate values

    check = probe.save_setup("w1", "probe", {"api_key": "good", "ratio": "0.2"}, {})
    assert check is not None and (check.ok, check.message) == (True, "Signed in as probe.")
    assert probe.setup_values("w1", "probe")[1] == {"api_key": "good", "ratio": 0.2}


def test_a_test_tool_that_cannot_run_saves_nothing(
    probe: Connectors, transport: FakeTransport
) -> None:
    transport.outputs["check_setup"] = [ConnectorError("probe did not start")]
    check = probe.save_setup("w1", "probe", {"api_key": "good"}, {})
    assert check is not None and not check.ok and "did not start" in check.message
    assert probe.setups.read() == {}


def test_credentials_live_in_a_mode_600_file_keyed_by_package_scope_and_key(
    probe: Connectors, tmp_path: Path
) -> None:
    probe.save_setup("w1", "probe", {"api_key": "good"}, {"voice": "high"})
    file = tmp_path / "setup.json"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert json.loads(file.read_text()) == {
        "probe": {"shared": {"api_key": "good"}, "worlds": {"w1": {"voice": "high"}}}
    }
    # An empty Credential keeps the saved one; None clears it.
    probe.save_setup("w1", "probe", {"api_key": "", "loud": "true"}, {})
    assert probe.setup_values("w1", "probe")[1] == {"api_key": "good", "loud": True}
    with pytest.raises(ValueError, match="API key: is required"):
        probe.save_setup("w1", "probe", {"api_key": None}, {})
    file.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        probe.setups.read()


def test_a_world_value_wins_over_the_shared_one_for_that_world_only(
    probe: Connectors, transport: FakeTransport
) -> None:
    probe.save_setup("w1", "probe", {"api_key": "shared-key"}, {})
    probe.save_setup("w2", "probe", {}, {"api_key": "w2-key", "voice": "high"})
    for world, key, voice in (("w1", "shared-key", "low"), ("w2", "w2-key", "high")):
        probe.run_stage(world, "r", f"s_{world}", {"calls": "probe", "params": {"tool": "x"}})
        env = transport.calls[-1][3]
        assert (env["PROBE_KEY"], env["VOICE"], env["RETRIES"], env["LOUD"]) == (
            key,
            voice,
            "3",
            "false",
        )
    probe.save_setup("w2", "probe", {}, {"voice": ""})  # clearing goes back to the default
    assert probe.setup_values("w2", "probe")[2] == {"api_key": "w2-key"}


def test_a_world_field_has_no_shared_value_and_unknown_fields_are_refused(
    probe: Connectors,
) -> None:
    with pytest.raises(ValueError, match="Voice: is set per world"):
        probe.save_setup("w1", "probe", {"api_key": "k", "voice": "high"}, {})
    with pytest.raises(ValueError, match="probe has no Setup field colour"):
        probe.save_setup("w1", "probe", {"api_key": "k"}, {"colour": "red"})
    with pytest.raises(ValueError, match="this world does not enable probe"):
        probe.save_setup("w3", "probe", {"api_key": "k"}, {})


def test_a_package_without_its_required_values_does_not_start(
    probe: Connectors, transport: FakeTransport
) -> None:
    connector = probe.find("w1", "probe")
    assert connector is not None and connector.credentials == ["PROBE_KEY"]
    assert probe.missing(connector) == ["api_key"]
    with pytest.raises(ConnectorError, match="needs its Setup on the Connectors page: api_key"):
        probe.run_stage("w1", "r", "s", {"calls": "probe"})
    probe.save_setup("w1", "probe", {"api_key": "k"}, {})
    assert probe.missing(connector) == []
    assert "PROBE_KEY" in probe.secret_names("w1")


def test_renamed_from_carries_a_value_across_a_rename(
    probe: Connectors, transport: FakeTransport
) -> None:
    # An earlier version called the field `server`.
    probe.setups.put("probe", "w1", {"api_key": "k", "server": "http://old:1"}, {})
    probe.run_stage("w1", "r", "s", {"calls": "probe", "params": {"tool": "x"}})
    assert transport.calls[-1][3]["ENDPOINT"] == "http://old:1"
    probe.save_setup("w1", "probe", {"ratio": "1"}, {})
    assert probe.setup_values("w1", "probe")[1] == {
        "api_key": "k",
        "endpoint": "http://old:1",
        "ratio": 1.0,
    }


def test_deleting_a_world_drops_its_values(probe: Connectors) -> None:
    probe.save_setup("w1", "probe", {"api_key": "k"}, {"voice": "high"})
    probe.delete_world("w1")
    assert probe.setups.read() == {"probe": {"shared": {"api_key": "k"}, "worlds": {}}}


def test_each_process_gets_exactly_its_own_values_and_never_another_packages(
    tmp_path: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARMIFY_LEAK_PROBE", "from the api process")
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    real.credentials.set({"FAL_KEY": "a V1 key"})
    install(real, tmp_path)
    install(real, tmp_path, "other", env="OTHER_KEY")
    for name in ("probe", "other"):
        real.enable("w1", name, "1.0.0")
    assert real.save_setup("w1", "probe", {"api_key": "good"}, {"cache": "/tmp/c"}) is not None
    real.setups.put("other", "w1", {"api_key": "other-secret", "ratio": 0.5}, {"voice": "high"})

    def environ(name: str) -> dict[str, str]:
        call = real.run_stage(
            "w1", "r", f"s_{name}", {"calls": name, "params": {"tool": "environ"}}
        )
        assert call.response is not None
        return call.response["env"]

    # Besides its own values a process has only what every process gets, and what the sandbox
    # sets (its own HOME and TMPDIR, the proxy): never another package's, nor the API's own.
    own = {"PROBE_KEY": "good", "RETRIES": "3", "LOUD": "false", "VOICE": "low", "CACHE": "/tmp/c"}
    theirs = {"OTHER_KEY": "other-secret", "RATIO": "0.5", "RETRIES": "3", "LOUD": "false"}
    theirs |= {"VOICE": "high"}
    for name, mine, foreign in (("probe", own, theirs), ("other", theirs, own)):
        env = environ(name)
        assert {k: env.get(k) for k in mine} == mine
        assert not set(env) & (set(foreign) - set(mine))
        assert not {foreign[k] for k in set(foreign) - set(mine)} & set(env.values())
        assert "SWARMIFY_LEAK_PROBE" not in env and "a V1 key" not in env.values()
