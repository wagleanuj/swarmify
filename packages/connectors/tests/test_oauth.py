"""OAuth sign-in as a Setup field (spec V2, OAuth): a fake provider drives sign-in, refresh,
revocation and the reauth mark a connector's auth failure sets."""

import json
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from swarmify_connectors import (
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
)
from swarmify_connectors.fake_oauth import FakeProvider
from swarmify_connectors.oauth import SignInNeeded, is_auth_failure
from swarmify_connectors.packages import MANIFEST

PROBE = Path(__file__).parent / "fixtures/packages/probe"
REDIRECT = "https://swarmify.tail1234.ts.net/api/setup/oauth/callback"


def install(
    connectors: Connectors,
    tmp_path: Path,
    provider: FakeProvider,
    finish: bool = False,
    settings: bool = False,
) -> None:
    """The probe package with a required per-world sign-in, `account`, at the fake provider;
    with `finish`, one its `signed_in` tool finishes (ADR 0029); with `settings`, one whose
    authorize URL carries the `notes` Setting as `login_hint`."""
    source = tmp_path / "src" / "probe"
    shutil.copytree(PROBE, source)
    toml = (source / MANIFEST).read_text()
    toml = toml.replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
    toml = toml.replace("required = true\n", "")  # only the sign-in is required here
    toml += f"""
[[config]]
key = "account"
label = "Account"
type = "oauth"
required = true
scope = "world"
env = "PROBE_TOKEN"
[config.oauth]
authorize_url = "{provider.url}/authorize"
token_url = "{provider.url}/token"
scopes = ["read", "write"]
pkce = true
client = "operator"
authorize_params = {{ access_type = "offline" }}
"""
    if finish:
        toml += "finish = true\n"
    if settings:
        toml += 'authorize_settings = { login_hint = "notes" }\n'
    (source / MANIFEST).write_text(toml)
    connectors.packages.install(str(source))
    connectors.enable("w1", "probe", "1.0.0")
    connectors.enable("w2", "probe", "1.0.0")


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    with FakeProvider() as p:
        yield p


@pytest.fixture
def probe(connectors: Connectors, tmp_path: Path, provider: FakeProvider) -> Connectors:
    install(connectors, tmp_path, provider)
    connectors.save_setup("w1", "probe", {"account_client_id": "app"}, {})
    connectors.save_setup("w1", "probe", {"account_client_secret": "shh"}, {})
    return connectors


@pytest.fixture
def finishing(connectors: Connectors, tmp_path: Path, provider: FakeProvider) -> Connectors:
    install(connectors, tmp_path, provider, finish=True)
    connectors.save_setup("w1", "probe", {"account_client_id": "app"}, {})
    connectors.save_setup("w1", "probe", {"account_client_secret": "shh"}, {})
    connectors.save_setup("w1", "probe", {"notes": "the page"}, {})
    return connectors


def sign_in(connectors: Connectors, world: str = "w1", session: str = "s1") -> None:
    """What the browser does: go to the provider, come back to the callback with its code."""
    url = connectors.start_sign_in(world, "probe", "account", REDIRECT, session)
    back = urlparse(httpx.get(url).headers["location"])
    assert back.geturl().startswith(REDIRECT)
    q = {k: v[0] for k, v in parse_qs(back.query).items()}
    connectors.finish_sign_in(connectors.sign_in_for(q["state"], session), q["code"])


def env(connectors: Connectors, transport: FakeTransport, world: str = "w1") -> dict[str, str]:
    connectors.run_stage(world, "r", f"s{len(transport.calls)}", {"calls": "probe"})
    return transport.calls[-1][3]


def test_the_authorize_url_asks_for_a_code_with_pkce_and_the_declared_scopes(
    probe: Connectors,
) -> None:
    url = urlparse(probe.start_sign_in("w1", "probe", "account", REDIRECT, "s1"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["response_type"] == "code" and q["client_id"] == "app"
    assert q["redirect_uri"] == REDIRECT and q["scope"] == "read write"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["access_type"] == "offline" and len(q["state"]) >= 32
    assert "shh" not in url.geturl()  # the client secret goes only to the token endpoint


def test_sign_in_stores_the_token_set_as_the_fields_credential_and_the_process_gets_it(
    probe: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    assert probe.missing(probe.find("w1", "probe")) == ["account"]  # type: ignore[arg-type]
    with pytest.raises(ConnectorError, match="needs its Setup on the Connectors page: account"):
        env(probe, transport)
    sign_in(probe)
    tokens = probe.setups.values("probe", "w1")[1]["account"]
    assert tokens["access_token"] in provider.access_tokens and tokens["refresh_token"]
    assert probe.missing(probe.find("w1", "probe")) == []  # type: ignore[arg-type]
    seen = env(probe, transport)
    assert seen["PROBE_TOKEN"] == tokens["access_token"]
    assert not any(k.endswith(("CLIENT_ID", "CLIENT_SECRET")) for k in seen)
    assert not any("shh" in v or v == tokens["refresh_token"] for v in seen.values())
    # a field set per world: the other world signs in on its own
    assert probe.missing(probe.find("w2", "probe")) == ["account"]  # type: ignore[arg-type]


def test_the_form_can_neither_type_a_token_nor_need_one_to_save_the_client(
    probe: Connectors,
) -> None:
    with pytest.raises(ValueError, match="Account: is set by signing in"):
        probe.save_setup("w1", "probe", {}, {"account": "typed"})
    assert probe.setup_values("w1", "probe")[1]["account_client_id"] == "app"


def test_a_sign_in_without_a_saved_client_id_is_refused(
    connectors: Connectors, tmp_path: Path, provider: FakeProvider
) -> None:
    install(connectors, tmp_path, provider)
    with pytest.raises(ValueError, match="Account: save its client ID first"):
        connectors.start_sign_in("w1", "probe", "account", REDIRECT, "s1")
    with pytest.raises(ValueError, match="no sign-in field api_key"):
        connectors.start_sign_in("w1", "probe", "api_key", REDIRECT, "s1")


def test_an_authorize_setting_reaches_the_url_the_worlds_value_winning(
    connectors: Connectors, tmp_path: Path, provider: FakeProvider
) -> None:
    install(connectors, tmp_path, provider, settings=True)
    connectors.save_setup("w1", "probe", {"account_client_id": "app", "notes": "shared"}, {})
    connectors.save_setup("w2", "probe", {}, {"notes": "own"})

    def hint(world: str) -> str:
        url = urlparse(connectors.start_sign_in(world, "probe", "account", REDIRECT, "s1"))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert q["access_type"] == "offline"  # beside the static authorize_params
        return q["login_hint"]

    assert (hint("w1"), hint("w2")) == ("shared", "own")


def test_a_sign_in_without_its_authorize_setting_is_refused_naming_it(
    connectors: Connectors, tmp_path: Path, provider: FakeProvider
) -> None:
    install(connectors, tmp_path, provider, settings=True)
    connectors.save_setup("w1", "probe", {"account_client_id": "app"}, {})
    with pytest.raises(ValueError, match="Account: set Notes first, then sign in"):
        connectors.start_sign_in("w1", "probe", "account", REDIRECT, "s1")


def test_state_is_one_time_and_bound_to_the_session_that_began_it(probe: Connectors) -> None:
    url = probe.start_sign_in("w1", "probe", "account", REDIRECT, "s1")
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(ValueError, match="stale or began in another browser"):
        probe.sign_in_for(state, "someone else")
    with pytest.raises(ValueError, match="stale"):
        probe.sign_in_for(state, "s1")  # used up by the failed try
    with pytest.raises(ValueError, match="stale"):
        probe.sign_in_for("made-up", "s1")
    url = probe.start_sign_in("w1", "probe", "account", REDIRECT, "s1")
    state = parse_qs(urlparse(url).query)["state"][0]
    probe._sign_ins[state]["until"] = time.monotonic() - 1
    with pytest.raises(ValueError, match="stale"):
        probe.sign_in_for(state, "s1")


def test_a_code_without_its_pkce_verifier_is_refused(probe: Connectors) -> None:
    url = probe.start_sign_in("w1", "probe", "account", REDIRECT, "s1")
    q = {k: v[0] for k, v in parse_qs(urlparse(httpx.get(url).headers["location"]).query).items()}
    pending = probe.sign_in_for(q["state"], "s1")
    with pytest.raises(SignInNeeded, match="invalid_grant"):
        probe.finish_sign_in({**pending, "verifier": "wrong"}, q["code"])
    assert "account" not in probe.setups.values("probe", "w1")[1]


def test_a_token_near_expiry_is_refreshed_before_the_process_gets_it(
    probe: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    provider.lifetime = 60  # inside the refresh margin: every start refreshes
    sign_in(probe)
    first = probe.setups.values("probe", "w1")[1]["account"]
    seen = env(probe, transport)["PROBE_TOKEN"]
    now = probe.setups.values("probe", "w1")[1]["account"]
    assert provider.grants == ["authorization_code", "refresh_token"]
    assert seen == now["access_token"] != first["access_token"]
    assert now["refresh_token"] != first["refresh_token"]  # the rotated one is kept
    provider.lifetime = 3600
    env(probe, transport)
    env(probe, transport)
    assert provider.grants.count("refresh_token") == 2  # a fresh token is used as it is


def test_a_revoked_sign_in_is_marked_and_signing_in_again_clears_it(
    probe: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    provider.lifetime = 0
    sign_in(probe)
    provider.revoke()
    with pytest.raises(ConnectorError, match="needs its Setup on the Connectors page: account"):
        env(probe, transport)
    connector = probe.find("w1", "probe")
    assert connector is not None and probe.sign_in_needed(connector) == ["account"]
    assert probe.missing(connector) == ["account"]
    provider.lifetime = 3600
    sign_in(probe)
    assert probe.sign_in_needed(connector) == [] and env(probe, transport)["PROBE_TOKEN"]


def test_a_connectors_auth_failure_marks_its_sign_in(
    probe: Connectors, transport: FakeTransport
) -> None:
    sign_in(probe)
    transport.outputs["probe"] = [ConnectorError("Error executing tool probe: auth_failed: no")]
    with pytest.raises(ConnectorError):
        env(probe, transport)
    connector = probe.find("w1", "probe")
    assert connector is not None and probe.sign_in_needed(connector) == ["account"]
    assert probe.sign_in_needed(probe.find("w2", "probe")) == []  # type: ignore[arg-type]


def test_another_failure_marks_nothing(probe: Connectors, transport: FakeTransport) -> None:
    sign_in(probe)
    transport.outputs["probe"] = [ConnectorError("probe: the disk is full")]
    with pytest.raises(ConnectorError):
        env(probe, transport)
    assert probe.sign_in_needed(probe.find("w1", "probe")) == []  # type: ignore[arg-type]


def test_an_unreachable_token_endpoint_is_an_error_not_a_reauth(
    probe: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    provider.lifetime = 0
    sign_in(probe)
    provider.server.shutdown()
    provider.server.server_close()
    with pytest.raises(ConnectorError, match="could not be reached"):
        env(probe, transport)
    assert probe.sign_in_needed(probe.find("w1", "probe")) == []  # type: ignore[arg-type]


def test_a_real_mcp_tool_error_reports_the_auth_failure(
    tmp_path: Path, db: Path, provider: FakeProvider
) -> None:
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    install(real, tmp_path, provider)
    real.setups.put("probe", "w1", {"account_client_id": "app", "account_client_secret": "shh"}, {})
    sign_in(real)
    with pytest.raises(ConnectorError, match="auth_failed"):
        real.run_stage("w1", "r", "s", {"calls": "probe", "params": {"tool": "refused"}})
    assert real.sign_in_needed(real.find("w1", "probe")) == ["account"]  # type: ignore[arg-type]


# A package that finishes its own sign-in (ADR 0029)


def test_a_finished_sign_in_stores_only_the_credential_signed_in_returns(
    finishing: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    transport.outputs["probe.signed_in"] = [{"access_token": "page-token", "expires_at": None}]
    sign_in(finishing)
    name, tool, arguments, process = transport.calls[-1]
    assert (name, tool) == ("probe", "signed_in")
    assert arguments["access_token"] in provider.access_tokens
    assert (arguments["client_id"], arguments["client_secret"]) == ("app", "shh")
    assert process["NOTES"] == "the page" and "PROBE_TOKEN" not in process
    assert finishing.setups.values("probe", "w1")[1]["account"] == {"access_token": "page-token"}
    # never refreshed: the connector gets it as it is, time after time
    assert env(finishing, transport)["PROBE_TOKEN"] == "page-token"
    assert env(finishing, transport)["PROBE_TOKEN"] == "page-token"
    assert provider.grants == ["authorization_code"]


def test_a_finished_credential_keeps_the_expiry_signed_in_gives(
    finishing: Connectors, transport: FakeTransport
) -> None:
    later = time.time() + 86400
    transport.outputs["probe.signed_in"] = [{"access_token": "page-token", "expires_at": later}]
    sign_in(finishing)
    stored = finishing.setups.values("probe", "w1")[1]["account"]
    assert stored == {"access_token": "page-token", "expires_at": later}


def test_a_failing_signed_in_stores_nothing_and_says_why(
    finishing: Connectors, transport: FakeTransport, provider: FakeProvider
) -> None:
    def fail(arguments: dict[str, Any]) -> dict[str, Any]:
        raise ConnectorError(f"probe: pick one of A or B (got {arguments['access_token']})")

    transport.outputs["probe.signed_in"] = [fail]
    with pytest.raises(ValueError, match="pick one of A or B") as raised:
        sign_in(finishing)
    assert not any(token in str(raised.value) for token in provider.access_tokens)
    assert "account" not in finishing.setups.values("probe", "w1")[1]
    transport.outputs["probe.signed_in"] = [{"expires_at": None}]
    with pytest.raises(ValueError, match="answered no Credential"):
        sign_in(finishing)
    assert "account" not in finishing.setups.values("probe", "w1")[1]


def test_an_auth_failed_signed_in_is_a_sign_in_needed_and_marks_nothing(
    finishing: Connectors, transport: FakeTransport
) -> None:
    transport.outputs["probe.signed_in"] = [{"access_token": "page-token", "expires_at": None}]
    sign_in(finishing)
    transport.outputs["probe.signed_in"] = [ConnectorError("probe: auth_failed: app removed")]
    with pytest.raises(SignInNeeded, match="auth_failed"):
        sign_in(finishing)
    assert finishing.setups.values("probe", "w1")[1]["account"] == {"access_token": "page-token"}


def test_two_worlds_finishing_a_world_sign_in_keep_their_own_credentials(
    finishing: Connectors, transport: FakeTransport
) -> None:
    transport.outputs["probe.signed_in"] = [
        {"access_token": "page-one", "expires_at": None},
        {"access_token": "page-two", "expires_at": None},
    ]
    sign_in(finishing, "w1")
    sign_in(finishing, "w2")
    assert env(finishing, transport, "w1")["PROBE_TOKEN"] == "page-one"
    assert env(finishing, transport, "w2")["PROBE_TOKEN"] == "page-two"


def test_auth_failed_from_a_call_marks_a_finished_sign_in(
    finishing: Connectors, transport: FakeTransport
) -> None:
    transport.outputs["probe.signed_in"] = [{"access_token": "page-token", "expires_at": None}]
    sign_in(finishing)
    transport.outputs["probe"] = [ConnectorError("Error executing tool probe: auth_failed: no")]
    with pytest.raises(ConnectorError):
        env(finishing, transport)
    connector = finishing.find("w1", "probe")
    assert connector is not None and finishing.sign_in_needed(connector) == ["account"]


def test_a_real_package_finishes_its_sign_in_in_its_own_process(
    tmp_path: Path, db: Path, provider: FakeProvider
) -> None:
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    install(real, tmp_path, provider, finish=True)
    real.setups.put("probe", "w1", {"account_client_id": "app", "account_client_secret": "shh"}, {})
    sign_in(real)
    token = real.setups.values("probe", "w1")[1]["account"]["access_token"]
    assert token.startswith("page-at-")


@pytest.mark.parametrize(
    ("message", "is_one"),
    [
        ("Error executing tool upload: auth_failed: token revoked", True),
        ("auth_failed", True),
        ("the auth_failed_count is 3", False),
        ("401 Unauthorized", False),
    ],
)
def test_the_auth_failure_signal(message: str, is_one: bool) -> None:
    assert is_auth_failure(message) is is_one
