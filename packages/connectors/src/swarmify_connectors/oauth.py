"""OAuth sign-in for a Setup field of type `oauth` (ADR 0024): the core, not the connector, runs
the authorization code flow and keeps the token set.

The operator registers their own app (`client = "operator"`); its client id and secret are two
shared Credentials of the package, `<key>_client_id` and `<key>_client_secret`. Sign-in sends
the browser to the field's `authorize_url` with a one-time `state` and, with `pkce`, an S256 code
challenge; the provider sends it back to Swarmify's own `/api/setup/oauth/callback`, which trades
the code for a token set at `token_url`. The token set is stored as the field's Credential:
`{"access_token", "refresh_token", "expires_at"}` plus `needs_sign_in` once the provider refuses
it. Token values never reach a log or an error message.
"""

import base64
import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

CALLBACK = "/api/setup/oauth/callback"
REFRESH_SECONDS = 300
"""A token closer than this to expiry is refreshed before a process gets it."""


class SignInNeeded(Exception):
    """The provider refused the stored token set: the operator signs in again."""


def client_fields(field: dict[str, Any]) -> list[dict[str, Any]]:
    """The two shared Credentials an oauth field's operator-registered app needs."""
    key, label = field["key"], field["label"]
    base = {"type": "string", "secret": True, "scope": "shared", "client_of": key}
    return [
        {**base, "key": f"{key}_client_id", "label": f"{label} client ID"},
        {
            **base,
            "key": f"{key}_client_secret",
            "label": f"{label} client secret",
            "help": "Leave empty for an app the provider issues no secret to.",
        },
    ]


def new_verifier() -> str:
    return secrets.token_urlsafe(48)


def authorize_url(
    field: dict[str, Any], client_id: str, redirect_uri: str, state: str, verifier: str
) -> str:
    o = field["oauth"]
    query = {
        **o.get("authorize_params", {}),
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if o.get("scopes"):
        query["scope"] = " ".join(o["scopes"])
    if o.get("pkce", True):
        digest = hashlib.sha256(verifier.encode()).digest()
        query["code_challenge"] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        query["code_challenge_method"] = "S256"
    sep = "&" if "?" in o["authorize_url"] else "?"
    return f"{o['authorize_url']}{sep}{urlencode(query)}"


def _token(field: dict[str, Any], client: tuple[str, str], form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint. Raises SignInNeeded when it refuses the grant, ValueError when
    it cannot be reached or answers nonsense; neither carries a token."""
    client_id, secret = client
    body = {**form, "client_id": client_id, **({"client_secret": secret} if secret else {})}
    try:
        answer = httpx.post(
            field["oauth"]["token_url"], data=body, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError as exc:
        raise ValueError(f"{field['label']}: the sign-in server could not be reached") from exc
    try:
        data = answer.json()
    except ValueError:
        data = {}
    if answer.status_code in (400, 401) and data.get("error"):
        # invalid_grant: revoked or expired; invalid_client: the app's id or secret changed
        raise SignInNeeded(f"{field['label']}: the provider said {data['error']}")
    if answer.status_code != 200 or not isinstance(data.get("access_token"), str):
        raise ValueError(f"{field['label']}: the sign-in server answered {answer.status_code}")
    tokens = {"access_token": data["access_token"]}
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    if isinstance(data.get("expires_in"), int | float):
        tokens["expires_at"] = time.time() + data["expires_in"]
    return tokens


def exchange(
    field: dict[str, Any], client: tuple[str, str], code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    """The token set an authorization code is worth."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if field["oauth"].get("pkce", True):
        form["code_verifier"] = verifier
    return _token(field, client, form)


def fresh(field: dict[str, Any], client: tuple[str, str], tokens: dict[str, Any]) -> dict[str, Any]:
    """The token set itself while its access token has more than REFRESH_SECONDS left, else a
    refreshed one (keeping the refresh token when the provider does not rotate it). Raises
    SignInNeeded when it cannot be refreshed."""
    if tokens.get("needs_sign_in"):
        raise SignInNeeded(f"{field['label']}: sign in again")
    expires = tokens.get("expires_at")
    if expires is None or expires - time.time() > REFRESH_SECONDS:
        return tokens
    if not tokens.get("refresh_token"):
        raise SignInNeeded(f"{field['label']}: the sign-in expired")
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    return {"refresh_token": tokens["refresh_token"], **_token(field, client, form)}


def is_auth_failure(message: str) -> bool:
    """A connector reports that the provider refused its access token by failing the tool call
    with an MCP tool error whose message holds the word `auth_failed`, as Python's MCP SDK does
    for `raise ToolError("auth_failed: the token was refused")`."""
    return re.search(r"\bauth_failed\b", message) is not None
