"""OAuth sign-in through HTTP: the redirect URI at the origin the browser used, the callback
bound to the session that began it, and the token set kept write-only."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from api_fakes import install
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmify_api import main
from swarmify_connectors import ConnectorError, FakeTransport
from swarmify_connectors.fake_oauth import FakeProvider

PASSWORD = "aviato"
PROBE = Path(__file__).parents[3] / "packages/connectors/tests/fixtures/packages/probe"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> FastAPI:
    """The API behind the operator's sign-in, with the fake transport. Replaces conftest's."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    monkeypatch.setenv("SWARMIFY_PASSWORD", PASSWORD)
    return main.create_app(tmp_path / "data", transport=transport, retro=False)


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    with FakeProvider() as p:
        yield p


def signed_in(app: FastAPI) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/session", json={"password": PASSWORD}).status_code == 204
    return client


def ready(client: TestClient, tmp_path: Path, provider: FakeProvider) -> str:
    """A world enabling the probe package with a shared `account` sign-in, its client saved."""
    source = tmp_path / "probe"
    shutil.copytree(PROBE, source)
    toml = source / "swarmify-connector.toml"
    toml.write_text(
        toml.read_text().replace("required = true\n", "")
        + f"""
[[config]]
key = "account"
label = "Account"
type = "oauth"
env = "PROBE_TOKEN"
[config.oauth]
authorize_url = "{provider.url}/authorize"
token_url = "{provider.url}/token"
pkce = true
"""
    )
    assert install(client, str(source)).status_code == 201
    w = client.post("/api/worlds", json={"name": "Signs in"}).json()["id"]
    client.put(f"/api/worlds/{w}/packages/probe", json={"version": "1.0.0"})
    body = {"shared": {"account_client_id": "app", "account_client_secret": "shh"}}
    assert client.put(f"/api/worlds/{w}/packages/probe/setup", json=body).status_code == 200
    return w


def test_sign_in_from_any_device_through_its_proxy(
    app: FastAPI, tmp_path: Path, provider: FakeProvider
) -> None:
    client = signed_in(app)
    w = ready(client, tmp_path, provider)
    form = client.get(f"/api/worlds/{w}/packages/probe/setup").json()
    by_key = {f["key"]: f for f in form["fields"]}
    assert [k for k in by_key if k.startswith("account")] == [
        "account_client_id", "account_client_secret", "account"
    ]  # fmt: skip
    assert by_key["account_client_id"]["env"] is None and by_key["account"]["secret"]
    assert (by_key["account"]["shared_set"], by_key["account"]["needs_sign_in"]) == (False, False)

    # Tailscale Serve (or Caddy) in front: the browser's origin is the forwarded one.
    started = client.post(
        f"/api/worlds/{w}/packages/probe/setup/oauth/account",
        headers={"X-Forwarded-Host": "box.tail1234.ts.net", "X-Forwarded-Proto": "https"},
    )
    assert started.status_code == 200, started.text
    redirect = "https://box.tail1234.ts.net/api/setup/oauth/callback"
    assert started.json()["redirect_uri"] == redirect
    back = urlparse(httpx.get(started.json()["url"]).headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == redirect

    stranger = signed_in(app)  # another browser, signed in too, cannot finish it
    foreign = stranger.get(f"/api/setup/oauth/callback?{back.query}", follow_redirects=False)
    assert foreign.status_code == 400 and "another browser" in foreign.json()["detail"]
    signed_out = TestClient(app).get(f"/api/setup/oauth/callback?{back.query}")
    assert signed_out.status_code == 401

    started = client.post(f"/api/worlds/{w}/packages/probe/setup/oauth/account")
    assert started.json()["redirect_uri"] == "http://testserver/api/setup/oauth/callback"
    back = urlparse(httpx.get(started.json()["url"]).headers["location"])
    done = client.get(f"/api/setup/oauth/callback?{back.query}", follow_redirects=False)
    assert done.status_code == 303
    assert done.headers["location"] == f"/worlds/{w}/connectors?sign_in=probe"
    again = client.get(f"/api/setup/oauth/callback?{back.query}", follow_redirects=False)
    assert again.status_code == 400  # a state works once

    form = client.get(f"/api/worlds/{w}/packages/probe/setup")
    assert (by_key := {f["key"]: f for f in form.json()["fields"]})["account"]["shared_set"]
    assert by_key["account"]["shared"] is None
    for token in provider.access_tokens | provider.refresh_tokens:
        assert token not in form.text
        assert token not in client.get(f"/api/worlds/{w}/connectors").text


def test_a_refused_consent_or_a_missing_client_says_why(
    app: FastAPI, tmp_path: Path, provider: FakeProvider
) -> None:
    client = signed_in(app)
    w = ready(client, tmp_path, provider)
    provider.deny = True
    started = client.post(f"/api/worlds/{w}/packages/probe/setup/oauth/account").json()
    back = urlparse(httpx.get(started["url"]).headers["location"])
    said = client.get(f"/api/setup/oauth/callback?{back.query}", follow_redirects=False)
    assert said.headers["location"] == (
        f"/worlds/{w}/connectors?sign_in_error=Not+signed+in%3A+you+said+no."
    )

    url = f"/api/worlds/{w}/packages/probe/setup"
    client.put(url, json={"shared": {"account_client_id": None}})
    refused = client.post(f"{url}/oauth/account")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Account: save its client ID first, then sign in"
    assert client.post(f"{url}/oauth/api_key").status_code == 422


def sign_in(client: TestClient, w: str) -> None:
    started = client.post(f"/api/worlds/{w}/packages/probe/setup/oauth/account").json()
    back = urlparse(httpx.get(started["url"]).headers["location"])
    done = client.get(f"/api/setup/oauth/callback?{back.query}", follow_redirects=False)
    assert "sign_in=probe" in done.headers["location"]


def test_a_connectors_auth_failure_shows_as_a_sign_in_to_do_again(
    app: FastAPI, tmp_path: Path, provider: FakeProvider, transport: FakeTransport
) -> None:
    client = signed_in(app)
    w = ready(client, tmp_path, provider)
    sign_in(client, w)

    def shown() -> tuple[list[str], bool]:
        rows = client.get(f"/api/worlds/{w}/connectors").json()
        form = client.get(f"/api/worlds/{w}/packages/probe/setup").json()
        field = next(f for f in form["fields"] if f["key"] == "account")
        return next(c for c in rows if c["name"] == "probe")["sign_in"], field["needs_sign_in"]

    assert shown() == ([], False)
    transport.outputs["check_setup"] = [
        ConnectorError("Error executing tool check_setup: auth_failed: the token was revoked")
    ]
    assert client.put(f"/api/worlds/{w}/packages/probe/setup", json={}).status_code == 422
    assert shown() == (["account"], True)
    sign_in(client, w)
    assert shown() == ([], False)
