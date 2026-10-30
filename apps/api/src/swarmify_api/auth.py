"""Operator sign-in (spec V2, Sign-in): one operator account per install, replacing basic auth.

The password is `SWARMIFY_PASSWORD`. Each start stores a fresh scrypt hash of it (a salt of its
own) when it does not match the stored one, so an install upgraded from basic auth signs in with
the password already in its `.env`, and changing it there signs every browser out. With no
password set or stored nothing is checked, which is right only on 127.0.0.1.

A browser signs in at `POST /api/session` and gets a session cookie: a random id signed with the
install's secret (HttpOnly, SameSite Lax, Secure over https), stored hashed with its expiry, so
signing out ends it. API tokens reach only the MCP server at `/mcp`: shown once, stored hashed,
revocable. The middleware is pure ASGI, so event streams pass through unbuffered. The skin's
static files, `/api/health` and `/api/session` stay open; every other `/api` path and `/mcp` is
refused without a session.
"""

import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from swarmify_kernel.ids import new_id
from swarmify_kernel.sqlite import connect

MIGRATIONS = Path(__file__).parent / "migrations"
COOKIE = "swarmify_session"
SESSION = timedelta(days=30)
MCP = "/mcp"
OPEN = frozenset({"/api/health", "/api/session"})
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def password_matches(password: str, stored: str) -> bool:
    _, salt, key = stored.split("$")
    given = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(given, bytes.fromhex(key))


def _digest(secret: str) -> str:
    """Sessions and tokens are long random strings, so a plain SHA-256 is enough to store."""
    return hashlib.sha256(secret.encode()).hexdigest()


class Token(BaseModel):
    id: str
    name: str
    created_at: str


class NewToken(Token):
    token: str
    """The secret, returned this once: only its hash is kept."""


class Operator:
    """The account, its sessions and its API tokens, in `swarmify.db`. One connection a call."""

    def __init__(self, db: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.db = db
        self.clock = clock

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with closing(connect(self.db)) as conn:
            yield conn

    def _account(self) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT password_hash, secret FROM operator").fetchone()

    @property
    def enabled(self) -> bool:
        return self._account() is not None

    def set_password(self, password: str) -> None:
        """Store `password` unless it already matches; a new one signs every session out."""
        account = self._account()
        if account and password_matches(password, account["password_hash"]):
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO operator VALUES (1, ?, ?)",
                (hash_password(password), secrets.token_hex(32)),
            )
            conn.execute("DELETE FROM sessions")

    def _sign(self, sid: str, secret: str) -> str:
        return hmac.new(secret.encode(), sid.encode(), hashlib.sha256).hexdigest()

    def sign_in(self, password: str) -> str | None:
        """A new session's cookie value for the right password, else None."""
        account = self._account()
        if account is None or not password_matches(password, account["password_hash"]):
            return None
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?)", (_digest(sid), (now + SESSION).isoformat())
            )
        return f"{sid}.{self._sign(sid, account['secret'])}"

    def session(self, cookie: str) -> bool:
        sid, _, signature = cookie.partition(".")
        account = self._account()
        expected = self._sign(sid, account["secret"]) if account else ""
        if not expected or not hmac.compare_digest(signature.encode(), expected.encode()):
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT expires_at FROM sessions WHERE hash = ?", (_digest(sid),)
            ).fetchone()
        return row is not None and datetime.fromisoformat(row["expires_at"]) > self.clock()

    def sign_out(self, cookie: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE hash = ?", (_digest(cookie.split(".")[0]),))

    def tokens(self) -> list[Token]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id, name, created_at FROM api_tokens ORDER BY id")
            return [Token(**dict(r)) for r in rows]

    def create_token(self, name: str) -> NewToken:
        made = NewToken(
            id=new_id("tok"),
            name=name,
            created_at=self.clock().isoformat(),
            token=f"swm_{secrets.token_urlsafe(32)}",
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_tokens VALUES (?, ?, ?, ?)",
                (made.id, name, _digest(made.token), made.created_at),
            )
        return made

    def revoke_token(self, token_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,)).rowcount > 0

    def token_valid(self, token: str) -> bool:
        with self._conn() as conn:
            found = conn.execute("SELECT 1 FROM api_tokens WHERE hash = ?", (_digest(token),))
            return found.fetchone() is not None


class Throttle:
    """At most `limit` wrong passwords from one client in `window` seconds."""

    # ponytail: keyed by the peer address, which behind Caddy is Caddy's, so the limit is the
    # install's; key by X-Forwarded-For from a trusted proxy if one attacker locking out the
    # operator for a few minutes ever matters.
    def __init__(self, limit: int = 10, window: float = 900.0) -> None:
        self.limit, self.window = limit, window
        self.failures: dict[str, deque[float]] = {}

    def wait(self, who: str) -> float:
        """Seconds until `who` may try again, 0 when they may now."""
        failures = self.failures.get(who, deque())
        while failures and failures[0] <= time.monotonic() - self.window:
            failures.popleft()
        if len(failures) < self.limit:
            return 0
        return failures[0] + self.window - time.monotonic()

    def failed(self, who: str) -> None:
        self.failures.setdefault(who, deque()).append(time.monotonic())


class SignedIn:
    """Refuses `/api` and `/mcp` without a session cookie; `/mcp` also takes a Bearer token."""

    def __init__(self, app: ASGIApp, operator: Operator) -> None:
        self.app = app
        self.operator = operator

    def allowed(self, scope: Scope) -> bool:
        path: str = scope["path"]
        mcp = path == MCP or path.startswith(MCP + "/")
        if path in OPEN or not (mcp or path == "/api" or path.startswith("/api/")):
            return True
        conn = HTTPConnection(scope)
        if (cookie := conn.cookies.get(COOKIE)) and self.operator.session(cookie):
            return True
        scheme, _, token = conn.headers.get("authorization", "").partition(" ")
        return mcp and scheme.lower() == "bearer" and self.operator.token_valid(token.strip())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or self.allowed(scope):
            return await self.app(scope, receive, send)
        await JSONResponse({"detail": "Sign in to Swarmify."}, 401)(scope, receive, send)


class SignIn(BaseModel):
    password: str = Field(max_length=1000)


class SessionView(BaseModel):
    signed_in: bool
    sign_in_required: bool
    """False when no password is set: every route is open, as on 127.0.0.1 natively."""


class TokenCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    """What the token is for, such as "Claude Code on the laptop"."""


def _https(request: Request) -> bool:
    # A forged header can only make the cookie stricter, so it needs no trusted proxy.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def router(operator: Operator, throttle: Throttle | None = None) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["auth"])
    throttle = throttle or Throttle()

    @api.get("/session")
    def get_session(request: Request) -> SessionView:
        if not operator.enabled:
            return SessionView(signed_in=True, sign_in_required=False)
        cookie = request.cookies.get(COOKIE)
        return SessionView(
            signed_in=bool(cookie and operator.session(cookie)), sign_in_required=True
        )

    @api.post("/session", status_code=204)
    def sign_in(body: SignIn, request: Request) -> Response:
        """Sign in with the operator password: sets the session cookie. 401 for a wrong
        password, 429 with Retry-After after too many."""
        who = request.client.host if request.client else ""
        if wait := throttle.wait(who):
            raise HTTPException(
                429,
                "Too many wrong passwords. Try again in a few minutes",
                {"Retry-After": str(math.ceil(wait))},
            )
        cookie = operator.sign_in(body.password)
        if cookie is None:
            throttle.failed(who)
            raise HTTPException(401, "That is not the password")
        response = Response(status_code=204)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=int(SESSION.total_seconds()),
            httponly=True,
            samesite="lax",
            secure=_https(request),
        )
        return response

    @api.delete("/session", status_code=204)
    def sign_out(request: Request) -> Response:
        if cookie := request.cookies.get(COOKIE):
            operator.sign_out(cookie)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, httponly=True, samesite="lax", secure=_https(request))
        return response

    @api.get("/tokens")
    def list_tokens() -> list[Token]:
        """API tokens for the MCP server, oldest first. Their secrets are never returned."""
        return operator.tokens()

    @api.post("/tokens", status_code=201)
    def create_token(body: TokenCreate) -> NewToken:
        """A new token for the MCP server at `/mcp`, sent as `Authorization: Bearer <token>`.
        The token is in this response only."""
        return operator.create_token(body.name)

    @api.delete("/tokens/{token_id}", status_code=204)
    def revoke_token(token_id: str) -> Response:
        if not operator.revoke_token(token_id):
            raise HTTPException(404, "token not found")
        return Response(status_code=204)

    return api
