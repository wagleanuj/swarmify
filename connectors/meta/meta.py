"""The Meta connector: a stdio MCP server over Meta's Graph API (ADR 0015), posting Reels to
Instagram and a Facebook Page as a Connector Package (ADR 0019).

The account is the Manifest's world-scoped `meta` oauth field, finished by this package (ADR
0029): Swarmify sends the operator to Facebook Login for Business, exchanges the code, then calls
`signed_in` with the short-lived user token and the Meta app's client ID and secret. `signed_in`
swaps it for a long-lived user token (`fb_exchange_token`), lists the person's Pages
(`me/accounts`) and answers the token of the one Page with a linked Instagram account. A Page
token made from a long-lived user token never expires, so it is kept with no expiry and never
refreshed. With several such Pages the `page` Setting (META_PAGE) names one by name or ID.

Every later call starts with that Page token as META_ACCESS_TOKEN and reads the Page ID and its
Instagram account ID with it (`account`). An answer that the token is invalid (code 190, or an
OAuthException) fails the tool with `auth_failed`, which marks the field for sign-in again.

`preview`, `publish` and `outcomes` are placeholders until publishing is built.
"""

import os
from collections.abc import Mapping
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

GRAPH = "https://graph.facebook.com/v26.0"
TOKEN = "META_ACCESS_TOKEN"
PAGE = "META_PAGE"
PAGE_FIELDS = "id,name,access_token,instagram_business_account"

mcp = MCPServer("meta")


# The Graph API


def _error(response: httpx.Response, what: str) -> ToolError:
    try:
        detail = response.json().get("error", {})
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {"message": str(detail)}
    message = detail.get("message") or response.text[:300]
    if detail.get("code") == 190 or detail.get("type") == "OAuthException":
        # the core marks the account for sign-in again
        return ToolError(f"auth_failed: {what} failed, Meta refused the sign-in: {message}")
    return ToolError(f"{what} failed ({response.status_code}): {message}")


def _get(
    http: httpx.Client, path: str, what: str, token: str = "", **params: str
) -> dict[str, Any]:
    """A Graph GET, its JSON answer. Tokens go in a header or the query, never in an error."""
    headers = {"Authorization": f"OAuth {token}"} if token else {}
    try:
        response = http.get(f"{GRAPH}/{path}", params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ToolError(f"{what} failed: Meta could not be reached") from exc
    if response.status_code != 200:
        raise _error(response, what)
    return response.json()


def _named(page: Mapping[str, Any]) -> str:
    return f"{page.get('name')} ({page.get('id')})"


def page_token(
    http: httpx.Client,
    env: Mapping[str, str],
    access_token: str,
    client_id: str,
    client_secret: str,
) -> str:
    """The token of the Page to post as: the one with a linked Instagram account, or the one
    META_PAGE names."""
    if not access_token:
        raise ToolError("auth_failed: Meta answered the sign-in with no access token")
    long_lived = _get(
        http,
        "oauth/access_token",
        "exchanging for a long-lived token",
        grant_type="fb_exchange_token",
        client_id=client_id,
        client_secret=client_secret,
        fb_exchange_token=access_token,
    )["access_token"]
    # ponytail: the first 100 Pages; follow paging.next for an account that manages more.
    pages = _get(
        http, "me/accounts", "listing your Pages", long_lived, fields=PAGE_FIELDS, limit="100"
    ).get("data", [])
    if not pages:
        raise ToolError("this Facebook account manages no Page; sign in with one that does")
    wanted = env.get(PAGE, "").strip()
    if wanted:
        chosen = [p for p in pages if wanted in (p.get("id"), p.get("name"))]
        if not chosen:
            raise ToolError(
                f"no Page {wanted!r} among this account's Pages: "
                f"{', '.join(_named(p) for p in pages)}"
            )
        if not chosen[0].get("instagram_business_account"):
            raise ToolError(
                f"the Page {_named(chosen[0])} has no linked Instagram account; link one in"
                " the Page's settings, then sign in again"
            )
        return str(chosen[0]["access_token"])
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise ToolError(
            f"none of this account's Pages has a linked Instagram account: "
            f"{', '.join(_named(p) for p in pages)}; link one in the Page's settings, then sign"
            " in again"
        )
    if len(linked) > 1:
        raise ToolError(
            f"{len(linked)} Pages have a linked Instagram account: "
            f"{', '.join(_named(p) for p in linked)}; set the Facebook Page Setting to the name"
            " or ID of the one to post to, then sign in again"
        )
    return str(linked[0]["access_token"])


@mcp.tool(
    description="Finish the Meta Sign-in: keep the token of the Page to post as, which never"
    " expires (ADR 0029)."
)
def signed_in(access_token: str, client_id: str = "", client_secret: str = "") -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as http:
        token = page_token(http, os.environ, access_token, client_id, client_secret)
    return {"access_token": token, "expires_at": None}


def account_of(http: httpx.Client, env: Mapping[str, str]) -> dict[str, str | None]:
    """The Page the token is for and its linked Instagram account (None without one)."""
    if not env.get(TOKEN):
        raise ToolError("auth_failed: sign in to Meta on the Connectors page")
    me = _get(http, "me", "reading the Page", env[TOKEN], fields="id,instagram_business_account")
    instagram = (me.get("instagram_business_account") or {}).get("id")
    return {"page": str(me["id"]), "instagram": str(instagram) if instagram else None}


@mcp.tool(description="The Facebook Page the connector posts as and its Instagram account.")
def account() -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as http:
        return account_of(http, os.environ)


# Publishing: placeholders until it is built


@mcp.tool(description="The fields a publish stage will post to Meta, and what would stop it.")
def preview(
    inputs: list[str] | None = None,
    params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"fields": [], "problems": ["posting to Meta is not built yet"]}


@mcp.tool(description="Post the video at `input_path` to Meta with the confirmed `fields`.")
def publish(fields: dict[str, Any], input_path: str, idempotency_key: str = "") -> dict[str, Any]:
    raise ToolError("posting to Meta is not built yet")


@mcp.tool(description="The Outcomes Capability: views of Meta posts, by post id.")
def outcomes(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {}


if __name__ == "__main__":  # pragma: no cover - started as a subprocess
    mcp.run("stdio")
