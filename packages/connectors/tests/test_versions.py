"""Versions and upgrades (issue 223): two versions of one package side by side; a run resolves
the version its workflow version recorded, a switch shows the Manifest diff and waits for a new
required Setup field, and an installed version can be removed."""

import pytest
from test_enable import FIXTURES

from swarmify_connectors import Connectors, FakeTransport, InstallError, Packages
from swarmify_connectors.packages import manifest_diff


@pytest.fixture
def packages(connectors: Connectors) -> Packages:
    installed = Packages(connectors.packages.root, FIXTURES)
    installed.install("packages/echo")
    installed.install("packages/echo_2")
    return installed


def test_a_run_resolves_the_recorded_version_not_the_worlds_pin(
    connectors: Connectors, packages: Packages, transport: FakeTransport
) -> None:
    connectors.enable("w1", "echo", "1.0.0")
    connectors.save_setup("w1", "echo", {"api_key": "k"}, {"region": "eu"}, "2.0.0")
    connectors.enable("w1", "echo", "2.0.0")
    transport.versions.clear()  # what the Setup test tool saw
    stage = {"calls": "echo", "params": {"tool": "echo"}}
    connectors.run_stage("w1", "r1", "s1", stage, pins={"echo": "1.0.0"})
    connectors.run_stage("w1", "r2", "s2", stage)
    assert transport.versions == ["1.0.0", "2.0.0"]
    pinned = connectors.find("w1", "echo", {"echo": "1.0.0"})
    assert pinned is not None and pinned.folder == str(packages.root / "echo/1.0.0")
    assert connectors.agent_servers("w1", ["echo"], {"echo": "1.0.0"})["echo"]["type"] == "stdio"

    connectors.disable("w1", "echo")  # a recorded version never brings back a disabled package
    assert connectors.find("w1", "echo", {"echo": "1.0.0"}) is None


def test_a_switch_shows_the_manifest_diff_and_waits_for_a_new_required_field(
    connectors: Connectors, packages: Packages
) -> None:
    first = connectors.switch("w1", "echo", "1.0.0")
    assert (first["from"], first["needs"]) == (None, [])  # a first enable never waits
    connectors.enable("w1", "echo", "1.0.0")

    diff = connectors.switch("w1", "echo", "2.0.0")
    assert (diff["from"], diff["to"]) == ("1.0.0", "2.0.0")
    assert diff["params"] == {"added": ["speed"], "removed": [], "changed": []}
    assert diff["setup"] == {"added": ["region"], "removed": [], "changed": []}
    assert diff["required"] == ["region"] and diff["needs"] == ["Region"]
    assert diff["permissions"] == {"added": ["network: eu.example.org"], "removed": []}
    with pytest.raises(ValueError, match="echo 2.0.0 needs a value for Region first"):
        connectors.enable("w1", "echo", "2.0.0")
    assert connectors.enabled("w1") == {"echo": "1.0.0"}

    with pytest.raises(ValueError, match="Region: is required"):  # its form is 2.0.0's
        connectors.save_setup("w1", "echo", {"api_key": "k"}, {}, "2.0.0")
    connectors.save_setup("w1", "echo", {"api_key": "k"}, {"region": "eu"}, "2.0.0")
    assert connectors.switch("w1", "echo", "2.0.0")["needs"] == []
    connectors.enable("w1", "echo", "2.0.0")
    back = connectors.switch("w1", "echo", "1.0.0")  # going back drops the field, never waits
    assert back["setup"]["removed"] == ["region"] and back["needs"] == []
    assert (
        connectors.pinned_by("echo", "2.0.0") == ["w1"]
        and connectors.pinned_by("echo", "1.0.0") == []
    )


def test_uninstall_removes_one_version_and_keeps_the_other(packages: Packages) -> None:
    packages.uninstall("echo", "1.0.0")
    assert [(p.name, p.version) for p in packages.list()] == [("echo", "2.0.0")]
    with pytest.raises(InstallError, match="echo 1.0.0 is not installed"):
        packages.uninstall("echo", "1.0.0")
    packages.uninstall("echo", "2.0.0")
    assert packages.list() == [] and not (packages.root / "echo").exists()


def test_a_renamed_or_defaulted_field_is_not_newly_required() -> None:
    old = {"config": [{"key": "token", "label": "Token", "type": "string", "required": True}]}
    new = {
        "config": [
            {
                "key": "api_token",
                "label": "Token",
                "type": "string",
                "required": True,
                "renamed_from": "token",
            },
            {
                "key": "region",
                "label": "Region",
                "type": "string",
                "required": True,
                "default": "eu",
            },
            {"key": "zone", "label": "Zone", "type": "string", "required": True},
        ]
    }
    assert manifest_diff(old, new)["required"] == ["zone"]
