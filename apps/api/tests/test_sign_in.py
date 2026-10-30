"""Operator sign-in: `SWARMIFY_PASSWORD` is the one operator's password; a browser signs in for a
signed session cookie, and API tokens open the MCP server at /mcp and nothing else."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from swarmify_api import main
from swarmify_api.auth import COOKIE, Operator
from swarmify_api.eval_harness import sign_in
from swarmify_api.main import migrated_db

PASSWORD = "aviato"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The API behind sign-in, serving a stand-in skin. Replaces conftest's, so live_url uses it."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    monkeypatch.setenv("SWARMIFY_PASSWORD", PASSWORD)
    return main.create_app(tmp_path / "data", retro=False)


def signed_in(client: TestClient) -> TestClient:
    assert client.post("/api/session", json={"password": PASSWORD}).status_code == 204
    return client


@pytest.mark.parametrize("path", ["/api/worlds", "/api/nope", "/api/tokens", "/api", "/mcp"])
def test_every_api_route_and_the_mcp_server_are_refused_without_a_session(
    client: TestClient, path: str
) -> None:
    for headers in ({}, {"Cookie": f"{COOKIE}=forged.0000"}, {"Authorization": "Basic ZTph"}):
        refused = client.get(path, headers=headers)
        assert refused.status_code == 401, path
        assert refused.json() == {"detail": "Sign in to Swarmify."}
        assert "www-authenticate" not in refused.headers, "no browser basic auth prompt"


def test_the_skin_health_and_sign_in_stay_open(client: TestClient) -> None:
    assert client.get("/").text == "<div id=root></div>"
    assert client.get("/worlds/world_abc").text == "<div id=root></div>"
    assert client.get("/api/health").json() == {"version": main.VERSION}
    assert client.get("/api/session").json() == {"signed_in": False, "sign_in_required": True}


def test_signing_in_sets_a_session_cookie_that_opens_the_api(client: TestClient) -> None:
    assert client.post("/api/session", json={"password": "wrong"}).status_code == 401
    made = client.post("/api/session", json={"password": PASSWORD})
    assert made.status_code == 204
    cookie = made.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    assert "Secure" not in cookie, "a LAN browser on plain http would drop a Secure cookie"
    assert client.get("/api/worlds").json() == []
    assert client.get("/api/session").json() == {"signed_in": True, "sign_in_required": True}


def test_the_cookie_is_secure_over_https(client: TestClient) -> None:
    made = client.post(
        "/api/session", json={"password": PASSWORD}, headers={"X-Forwarded-Proto": "https"}
    )
    assert "Secure" in made.headers["set-cookie"]


def test_signing_out_ends_the_session_even_for_a_copied_cookie(client: TestClient) -> None:
    signed_in(client)
    copied = client.cookies[COOKIE]
    assert client.delete("/api/session").status_code == 204
    assert COOKIE not in client.cookies
    assert client.get("/api/worlds").status_code == 401
    client.cookies.set(COOKIE, copied)
    assert client.get("/api/worlds").status_code == 401


def test_a_cookie_with_a_wrong_signature_is_refused(client: TestClient) -> None:
    signed_in(client)
    sid, _, signature = client.cookies[COOKIE].partition(".")
    client.cookies.clear()
    client.cookies.set(COOKIE, f"{sid}.{'0' * len(signature)}")
    assert client.get("/api/worlds").status_code == 401


def test_a_garbled_cookie_is_refused_not_an_error(tmp_path: Path) -> None:
    operator = Operator(migrated_db(tmp_path))
    operator.set_password(PASSWORD)
    for cookie in ("", ".", "nodot", "a.é", "a.b.c"):
        assert not operator.session(cookie)


def test_a_session_expires_after_thirty_days(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 30, tzinfo=UTC)]
    operator = Operator(migrated_db(tmp_path), clock=lambda: now[0])
    operator.set_password(PASSWORD)
    cookie = operator.sign_in(PASSWORD)
    assert cookie and operator.session(cookie)
    now[0] += timedelta(days=29)
    assert operator.session(cookie)
    now[0] += timedelta(days=1)
    assert not operator.session(cookie)


def test_the_password_is_stored_as_a_salted_scrypt_hash(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    one, two = Operator(db), Operator(migrated_db(tmp_path / "other"))
    one.set_password(PASSWORD)
    two.set_password(PASSWORD)
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT password_hash FROM operator").fetchone()[0]
    with sqlite3.connect(tmp_path / "other" / "swarmify.db") as conn:
        other = conn.execute("SELECT password_hash FROM operator").fetchone()[0]
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert stored != other, "each hash has its own salt"


def test_an_upgraded_install_signs_in_with_its_env_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop's `.env` keeps SWARMIFY_PASSWORD: the first start after the upgrade makes it
    the operator password, a restart keeps every session, and a new one in `.env` ends them."""
    monkeypatch.setenv("SWARMIFY_PASSWORD", PASSWORD)
    cookies = signed_in(TestClient(main.create_app(tmp_path))).cookies
    again = TestClient(main.create_app(tmp_path), cookies=cookies)
    assert again.get("/api/worlds").status_code == 200
    monkeypatch.setenv("SWARMIFY_PASSWORD", "hooli")
    changed = TestClient(main.create_app(tmp_path), cookies=cookies)
    assert changed.get("/api/worlds").status_code == 401
    assert changed.post("/api/session", json={"password": PASSWORD}).status_code == 401
    assert changed.post("/api/session", json={"password": "hooli"}).status_code == 204


def test_too_many_wrong_passwords_are_refused_for_a_while(client: TestClient) -> None:
    for _ in range(10):
        assert client.post("/api/session", json={"password": "wrong"}).status_code == 401
    blocked = client.post("/api/session", json={"password": PASSWORD})
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 900


def test_a_token_is_shown_once_and_never_opens_the_http_api(client: TestClient) -> None:
    signed_in(client)
    made = client.post("/api/tokens", json={"name": "  Claude Code  "})
    assert made.status_code == 201
    token = made.json()
    assert token["name"] == "Claude Code" and token["token"].startswith("swm_")
    assert client.get("/api/tokens").json() == [
        {"id": token["id"], "name": "Claude Code", "created_at": token["created_at"]}
    ]
    assert client.post("/api/tokens", json={"name": " "}).status_code == 422
    bearer = {"Authorization": f"Bearer {token['token']}"}
    client.cookies.clear()
    assert client.get("/api/worlds", headers=bearer).status_code == 401
    assert client.get("/api/tokens", headers=bearer).status_code == 401


async def _list_worlds(url: str, token: str) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(f"{url}/mcp", http_client=http)) as mcp,
    ):
        result = await mcp.call_tool("create_world", {"name": "From a script"})
        assert not result.is_error
        listed = await mcp.call_tool("list_worlds", {})
        return [block.text for block in listed.content]  # type: ignore[union-attr]


def test_a_token_drives_the_mcp_server_until_it_is_revoked(live_url: str) -> None:
    with httpx.Client(base_url=live_url) as http:
        http.post("/api/session", json={"password": PASSWORD}).raise_for_status()
        token = http.post("/api/tokens", json={"name": "slack approvals"}).json()
        assert "From a script" in "".join(anyio.run(_list_worlds, live_url, token["token"]))
        assert [w["name"] for w in http.get("/api/worlds").json()] == ["From a script"]
        assert http.delete(f"/api/tokens/{token['id']}").status_code == 204
        assert http.delete(f"/api/tokens/{token['id']}").status_code == 404
        assert http.get("/api/tokens").json() == []
    refused = httpx.post(
        f"{live_url}/mcp",
        headers={"Authorization": f"Bearer {token['token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert refused.status_code == 401


def test_no_password_means_no_sign_in_for_a_native_mac(tmp_path: Path) -> None:
    client = TestClient(main.create_app(tmp_path))
    assert client.get("/api/worlds").status_code == 200
    assert client.get("/api/session").json() == {"signed_in": True, "sign_in_required": False}


def test_the_eval_harness_signs_in_like_a_browser(client: TestClient) -> None:
    sign_in(client, PASSWORD)  # type: ignore[arg-type]
    assert client.get("/api/worlds").status_code == 200
