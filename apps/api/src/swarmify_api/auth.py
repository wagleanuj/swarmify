"""Basic auth for the LAN (spec: proper auth is V2). On when `SWARMIFY_PASSWORD` is set; the
user is `SWARMIFY_USER`, `swarmify` by default. A pure ASGI middleware, so event streams pass
through unbuffered. `/api/health` stays open for the container healthcheck; it shows only the
version."""

import base64
import binascii
import secrets

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

OPEN = frozenset({"/api/health"})


class BasicAuth:
    def __init__(self, app: ASGIApp, user: str, password: str) -> None:
        self.app = app
        self.expected = f"{user}:{password}".encode()

    def allowed(self, scope: Scope) -> bool:
        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, token = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            given = base64.b64decode(token, validate=True)
        except binascii.Error:
            return False
        return secrets.compare_digest(given, self.expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN or self.allowed(scope):
            return await self.app(scope, receive, send)
        refused = PlainTextResponse(
            "Sign in to Swarmify.", 401, {"WWW-Authenticate": 'Basic realm="Swarmify"'}
        )
        await refused(scope, receive, send)
