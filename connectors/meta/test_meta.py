"""The Meta connector: its Sign-in finish (`signed_in`) and per-call account lookup against a fake
Graph API (httpx's MockTransport, no network), then the package installed through the real
install path, given to a world as a default and answering over stdio MCP in its sandbox."""

from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import meta
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from swarmify_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from swarmify_connectors import oauth as core_oauth
from swarmify_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from swarmify_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent


def page(n: int, instagram: bool = True) -> dict[str, Any]:
    linked = {"instagram_business_account": {"id": f"ig{n}"}} if instagram else {}
    return {"id": f"p{n}", "name": f"Page {n}", "access_token": f"page-tok-{n}", **linked}


class FakeMeta:
    """The Graph API: the long-lived exchange, `me/accounts` answering `pages`, and `me` as the
    Page a Page token is for. `refused` answers every call with Meta's invalid-token error."""

    def __init__(self, pages: list[dict[str, Any]], refused: bool = False) -> None:
        self.pages = pages
        self.refused = refused
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.refused:
            error = {"message": "Error validating access token", "type": "OAuthException"}
            return httpx.Response(400, json={"error": {**error, "code": 190}})
        path = request.url.path
        if path == "/v26.0/oauth/access_token":
            return httpx.Response(200, json={"access_token": "long-tok", "expires_in": 5183944})
        if path == "/v26.0/me/accounts":
            return httpx.Response(200, json={"data": self.pages})
        if path == "/v26.0/me":
            return httpx.Response(
                200, json={"id": "p1", "instagram_business_account": {"id": "ig1"}}
            )
        return httpx.Response(404, json={"error": {"message": "unknown path", "code": 803}})

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def sign_in(fake: FakeMeta, env: dict[str, str] | None = None) -> str:
    with fake.http() as http:
        return meta.page_token(http, env or {}, "short-tok", "app-id", "app-secret")


# signed_in: the long-lived exchange, then the Page to keep


def test_the_one_page_with_instagram_is_kept_after_the_long_lived_exchange() -> None:
    fake = FakeMeta([page(1, instagram=False), page(2)])
    assert sign_in(fake) == "page-tok-2"
    exchange, accounts = fake.requests
    assert exchange.method == "GET" and exchange.url.path == "/v26.0/oauth/access_token"
    assert dict(exchange.url.params) == {
        "grant_type": "fb_exchange_token",
        "client_id": "app-id",
        "client_secret": "app-secret",
        "fb_exchange_token": "short-tok",
    }
    assert accounts.url.params["fields"] == "id,name,access_token,instagram_business_account"
    assert accounts.headers["authorization"] == "OAuth long-tok"  # the long-lived token


@pytest.mark.parametrize("named", ["Page 3", "p3"])
def test_with_several_pages_the_page_setting_picks_one_by_name_or_id(named: str) -> None:
    assert sign_in(FakeMeta([page(1), page(2), page(3)]), {"META_PAGE": named}) == "page-tok-3"


def test_with_several_pages_and_no_setting_it_fails_naming_them() -> None:
    with pytest.raises(ToolError) as several:
        sign_in(FakeMeta([page(1), page(2), page(3, instagram=False)]))
    message = str(several.value)
    assert "2 Pages have a linked Instagram account: Page 1 (p1), Page 2 (p2)" in message
    assert "set the Facebook Page Setting" in message and "Page 3" not in message
    with pytest.raises(ToolError, match=r"no Page 'Page 9' among .*Page 1 \(p1\), Page 2 \(p2\)"):
        sign_in(FakeMeta([page(1), page(2)]), {"META_PAGE": "Page 9"})


def test_a_page_without_a_linked_instagram_account_is_refused() -> None:
    lonely = FakeMeta([page(1), page(2, instagram=False)])
    with pytest.raises(ToolError, match=r"the Page Page 2 \(p2\) has no linked Instagram account"):
        sign_in(lonely, {"META_PAGE": "p2"})
    with pytest.raises(ToolError, match=r"none of this account's Pages .*: Page 1 \(p1\)"):
        sign_in(FakeMeta([page(1, instagram=False)]))
    with pytest.raises(ToolError, match="manages no Page"):
        sign_in(FakeMeta([]))


def test_meta_refusing_the_token_is_an_auth_failure_that_never_shows_a_token() -> None:
    """The word the core reads to mark the account for sign-in again (oauth.is_auth_failure)."""
    with pytest.raises(ToolError) as refused:
        sign_in(FakeMeta([page(1)], refused=True))
    assert core_oauth.is_auth_failure(str(refused.value))
    assert "Error validating access token" in str(refused.value)
    assert "tok" not in str(refused.value).replace("token", "")
    with FakeMeta([page(1)], refused=True).http() as http:
        with pytest.raises(ToolError) as later:
            meta.account_of(http, {"META_ACCESS_TOKEN": "page-tok-1"})
    assert core_oauth.is_auth_failure(str(later.value))
    with FakeMeta([]).http() as http:
        with pytest.raises(ToolError) as unsigned:
            meta.account_of(http, {})
    assert core_oauth.is_auth_failure(str(unsigned.value))


def test_other_meta_errors_are_plain_tool_errors() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "try later", "code": 2}})

    with httpx.Client(transport=httpx.MockTransport(down)) as http:
        with pytest.raises(ToolError, match=r"long-lived token failed \(500\): try later") as e:
            meta.page_token(http, {}, "short-tok", "app-id", "app-secret")
    assert not core_oauth.is_auth_failure(str(e.value))


# At each call: the Page and its Instagram account, read with the Page token


def test_each_call_reads_the_page_and_its_instagram_account_with_the_page_token() -> None:
    fake = FakeMeta([])
    with fake.http() as http:
        assert meta.account_of(http, {"META_ACCESS_TOKEN": "page-tok-1"}) == {
            "page": "p1",
            "instagram": "ig1",
        }
    (me,) = fake.requests
    assert me.url.path == "/v26.0/me" and me.url.params["fields"] == "id,instagram_business_account"
    assert me.headers["authorization"] == "OAuth page-tok-1"


# The Manifest, and the package installed as Swarmify does at startup


def test_the_manifest_declares_a_world_sign_in_the_package_finishes() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["stage_types"]) == ("meta", "publish", ["connector"])
    assert (m["capabilities"], m["primary_metric"]) == (["publish", "outcomes"], "views")
    account, chosen = m["config"]
    assert (account["key"], account["type"], account["env"]) == ("meta", "oauth", meta.TOKEN)
    assert (account["scope"], chosen["scope"]) == ("world", "world")
    assert (chosen["key"], chosen["type"], chosen["env"]) == ("page", "string", meta.PAGE)
    assert not chosen.get("required")
    o = account["oauth"]
    assert (o["authorize_url"], o["token_url"]) == (
        "https://www.facebook.com/v26.0/dialog/oauth",
        "https://graph.facebook.com/v26.0/oauth/access_token",
    )
    assert (o["pkce"], o["finish"], o.get("scopes")) == (False, True, None)
    url = core_oauth.authorize_url(account, "app-id", "https://box.ts.net/cb", "st", "v" * 43)
    assert "config_id=" in url and "code_challenge" not in url and "scope=" not in url
    assert m["permissions"] == {"network": ["graph.facebook.com", "rupload.facebook.com"]}


def registry(data: Path) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), McpTransport(), Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


def test_the_installed_package_answers_over_stdio_in_its_sandbox(tmp_path: Path) -> None:
    connectors = registry(tmp_path)
    assert connectors.install_defaults()["meta"] == "1.0.0"
    connectors.offer_defaults("w")
    found = connectors.find("w", "meta")
    assert found is not None and found.version == "1.0.0"
    assert sorted(t.name for t in connectors.transport.list_tools(found, {})) == [
        "account",
        "outcomes",
        "preview",
        "publish",
        "signed_in",
    ]
    env = connectors._env(found, complete=False)
    # no token from Meta: refused before reaching it, as the core's Sign-in error
    with pytest.raises(ConnectorError) as refused:
        arguments = {"access_token": "", "client_id": "app-id", "client_secret": "s"}
        connectors._tool(found, "signed_in", arguments, env)
    assert core_oauth.is_auth_failure(str(refused.value))

    def stage(**params: Any) -> dict[str, Any]:
        return {"calls": "meta", "input": "short.mp4", "params": params}

    # only a Facebook draft posts privately; a stage with Instagram is always gated
    assert connectors.posts_privately("w", stage(platforms=["facebook"], visibility="draft"))
    assert not connectors.posts_privately("w", stage(visibility="draft"))
    both = stage(platforms=["instagram", "facebook"], visibility="draft")
    assert not connectors.posts_privately("w", both)
