"""A fake OAuth 2.0 provider on a local port, for tests: it consents at once, checks PKCE, the
client, the redirect URI and the one-time code, rotates refresh tokens, and can revoke them all.

    with FakeProvider() as provider:
        provider.url  # http://127.0.0.1:<port>, serving /authorize and /token
"""

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs, urlencode, urlparse


class FakeProvider:
    def __init__(
        self, client_id: str = "app", client_secret: str = "shh", lifetime: int = 3600
    ) -> None:
        self.client = (client_id, client_secret)
        self.lifetime = lifetime
        """The `expires_in` of each access token it issues."""
        self.deny = False
        """When set, the operator says no at the consent screen."""
        self.codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()
        self.grants: list[str] = []
        """Each token request's grant type, in order."""
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path != "/authorize" or q.get("client_id") != provider.client[0]:
                    return self._send(400, {"error": "invalid_client"})
                if provider.deny:
                    back = {"error": "access_denied", "state": q["state"]}
                else:
                    code = secrets.token_urlsafe(8)
                    provider.codes[code] = q
                    back = {"code": code, "state": q["state"]}
                self.send_response(302)
                self.send_header("Location", f"{q['redirect_uri']}?{urlencode(back)}")
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                f = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                provider.grants.append(f.get("grant_type", ""))
                if (f.get("client_id"), f.get("client_secret")) != provider.client:
                    return self._send(401, {"error": "invalid_client"})
                if f.get("grant_type") == "authorization_code":
                    asked = provider.codes.pop(f.get("code", ""), None)
                    verifier = f.get("code_verifier", "").encode()
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest())
                    if (
                        asked is None
                        or asked["redirect_uri"] != f.get("redirect_uri")
                        or asked.get("code_challenge") != challenge.rstrip(b"=").decode()
                    ):
                        return self._send(400, {"error": "invalid_grant"})
                elif f.get("grant_type") == "refresh_token":
                    if f.get("refresh_token") not in provider.refresh_tokens:
                        return self._send(400, {"error": "invalid_grant"})
                    provider.refresh_tokens.discard(f["refresh_token"])
                else:
                    return self._send(400, {"error": "unsupported_grant_type"})
                access, refresh = f"at-{secrets.token_hex(4)}", f"rt-{secrets.token_hex(4)}"
                provider.access_tokens.add(access)
                provider.refresh_tokens.add(refresh)
                self._send(
                    200,
                    {
                        "access_token": access,
                        "refresh_token": refresh,
                        "expires_in": provider.lifetime,
                        "token_type": "Bearer",
                    },
                )

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def revoke(self) -> None:
        """The account owner revokes the app: every token it holds stops working."""
        self.refresh_tokens.clear()
        self.access_tokens.clear()

    def __enter__(self) -> Self:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
