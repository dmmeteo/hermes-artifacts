"""The optional standalone console: the shared panel plus its browser API, behind one Basic login.

The guard owns every /console request before FastAPI sees it: console enabled, request Host is the
configured origin, Basic credentials verify against the configured bcrypt hash, and browser API calls
carry the panel's request header (writes also need the exact Origin). Proxy identity headers are never
trusted. Artifact decisions stay in the shared operations the management API also uses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from importlib.resources import files

import anyio
import bcrypt
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import ConsoleSettings

PREFIX = "/console"
API_HEADER = "x-artifacts-console"
PANEL = files("app") / "console"
ASSETS = {"console.js": "text/javascript", "console.css": "text/css"}
REALM = 'Basic realm="Hermes Artifacts", charset="UTF-8"'
SHELL_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
MAX_BODY = 16 * 1024
FRAME_PATH = re.compile(PREFIX + r"/api/artifacts/[0-9A-Fa-f-]{36}/frame")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class BasicAuth:
    """bcrypt is deliberately slow, so a verified header is remembered briefly by a keyed digest."""

    def __init__(self, settings: ConsoleSettings, clock: Callable[[], float] = time.monotonic,
                 ttl: float = 300.0, attempts_per_minute: int = 20, all_attempts_per_minute: int = 120):
        self.settings, self.clock, self.ttl = settings, clock, ttl
        self._key = secrets.token_bytes(32)
        self._verified: OrderedDict[bytes, float] = OrderedDict()
        self._attempts: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self.attempts_per_minute = attempts_per_minute
        # A forged or hidden client address must not buy unlimited bcrypt work: one budget covers everyone.
        self._everyone = (float(all_attempts_per_minute), clock())
        self.all_attempts_per_minute = all_attempts_per_minute
        self.slots = anyio.Semaphore(2)

    def allow_globally(self) -> bool:
        now, rate = self.clock(), self.all_attempts_per_minute / 60.0
        tokens, last = self._everyone
        tokens = min(float(self.all_attempts_per_minute), tokens + (now - last) * rate)
        allowed = tokens >= 1
        self._everyone = (tokens - 1 if allowed else tokens, now)
        return allowed

    def _digest(self, header: str) -> bytes:
        return hmac.new(self._key, header.encode("latin-1", "replace"), hashlib.sha256).digest()

    def cached(self, header: str) -> bool:
        expiry = self._verified.get(self._digest(header))
        return expiry is not None and expiry > self.clock()

    def allow_attempt(self, client: str) -> bool:
        now, rate = self.clock(), self.attempts_per_minute / 60.0
        tokens, last = self._attempts.pop(client, (float(self.attempts_per_minute), now))
        tokens = min(float(self.attempts_per_minute), tokens + (now - last) * rate)
        allowed = tokens >= 1
        self._attempts[client] = (tokens - 1 if allowed else tokens, now)
        while len(self._attempts) > 10_000:
            self._attempts.popitem(last=False)
        return allowed

    def verify(self, header: str) -> bool:
        credentials = parse_basic(header)
        if credentials is None:
            return False
        user, password = credentials
        user_ok = hmac.compare_digest(user.encode(), self.settings.username.encode())
        try:
            password_ok = bcrypt.checkpw(password, self.settings.password_hash)
        except ValueError:
            password_ok = False
        if user_ok and password_ok:
            self._verified[self._digest(header)] = self.clock() + self.ttl
            while len(self._verified) > 256:
                self._verified.popitem(last=False)
            return True
        return False


def parse_basic(header: str) -> tuple[str, bytes] | None:
    """`Basic base64(user:password)`, UTF-8; anything malformed or oversized is no credential at all."""
    scheme, _, encoded = header.strip().partition(" ")
    if scheme.lower() != "basic" or not encoded or len(encoded) > 512:
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    raw = password.encode("utf-8")
    if not sep or not user or not raw or len(raw) > 72 or "\x00" in decoded:
        return None
    return user, raw


class ConsoleGuard:
    """ASGI gate for /console. Disabled, wrong host and unauthenticated requests never reach a route."""

    def __init__(self, app, console: ConsoleSettings | None, unavailable: Callable[[], Response]):
        self.app, self.console, self.unavailable = app, console, unavailable
        self.auth = BasicAuth(console) if console else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (scope["path"] == PREFIX or scope["path"].startswith(PREFIX + "/")):
            return await self.app(scope, receive, send)
        refusal = await self.refusal(scope)
        if refusal is not None:
            return await refusal(scope, receive, send)
        if not scope["path"].startswith(PREFIX + "/api/"):
            return await self.app(scope, receive, send)

        async def private(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                message["headers"] = headers + [(name.lower().encode(), value.encode())
                                                for name, value in API_HEADERS.items()
                                                if name.lower().encode() not in names]
            await send(message)

        return await self.app(scope, receive, private)

    async def refusal(self, scope) -> Response | None:
        if self.console is None:
            return self.unavailable()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "").lower()
        if host.removesuffix(":443" if self.console.origin.startswith("https:") else ":80") != self.console.host:
            return self.unavailable()
        api = scope["path"].startswith(PREFIX + "/api/")
        authorization = headers.get("authorization", "")
        if not self.auth.cached(authorization):
            client = (scope.get("client") or ("unknown", 0))[0]
            busy = self.auth.slots.value == 0
            if authorization and (busy or not self.auth.allow_attempt(client) or not self.auth.allow_globally()):
                return _refuse(api, 429, "too_many_attempts", "too many login attempts; wait a minute",
                               {"Retry-After": "60"})
            async with self.auth.slots:
                verified = await run_in_threadpool(self.auth.verify, authorization)
            if not verified:
                return _refuse(api, 401, "unauthorized", "sign in with the console credentials",
                               {"WWW-Authenticate": REALM})
        if not api:
            return None if scope["method"] in ("GET", "HEAD") else _refuse(False, 405, "method_not_allowed", "")
        length = headers.get("content-length", "")
        if length and (not length.isascii() or not length.isdigit() or int(length) > MAX_BODY):
            return _refuse(True, 413, "too_large", "request body is too large")
        if "transfer-encoding" in headers:
            return _refuse(True, 411, "length_required", "send the request body with a Content-Length")
        if FRAME_PATH.fullmatch(scope["path"]) and scope["method"] in ("GET", "HEAD"):
            # An iframe navigation cannot carry the API header; it must come from the console's own page.
            if headers.get("sec-fetch-dest") != "iframe" or headers.get("sec-fetch-site") != "same-origin":
                return _refuse(True, 403, "forbidden", "the preview frame opens only inside the console")
            return None
        if headers.get(API_HEADER) != "1":
            return _refuse(True, 403, "forbidden", "console API calls come only from the console page")
        if headers.get("sec-fetch-site") not in (None, "same-origin"):
            return _refuse(True, 403, "forbidden", "cross-site console requests are refused")
        if scope["method"] in UNSAFE and headers.get("origin") != self.console.origin:
            return _refuse(True, 403, "forbidden", "writes must come from the configured console origin")
        return None


def _refuse(api: bool, status: int, code: str, message: str, extra: dict | None = None) -> Response:
    headers = {**PRIVATE_HEADERS, **(extra or {})}
    if api:
        return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)
    return HTMLResponse(f"<!doctype html><title>Hermes Artifacts</title><p>{message or 'Not allowed'}.</p>",
                        status, headers={**headers, "Content-Security-Policy": "default-src 'none'"})


def console_router(ops) -> APIRouter:
    """The shell, its two assets and the panel's API; every decision is one of the shared operations."""
    router = APIRouter(prefix=PREFIX, include_in_schema=False)

    def shell() -> HTMLResponse:
        body = (PANEL / "console.html").read_bytes()
        return HTMLResponse(body, headers={**PRIVATE_HEADERS, "Content-Security-Policy": SHELL_CSP})

    @router.api_route("", methods=["GET", "HEAD"])
    def bare():
        # The page loads its assets relative to /console/.
        return RedirectResponse(f"{PREFIX}/", 308, headers=PRIVATE_HEADERS)

    router.add_api_route("/", shell, methods=["GET", "HEAD"])

    @router.api_route("/assets/{name}", methods=["GET", "HEAD"])
    def asset(name: str):
        media = ASSETS.get(name)
        if media is None:
            return JSONResponse({"error": {"code": "not_found", "message": "not found"}}, 404)
        return Response((PANEL / name).read_bytes(), media_type=media,
                        headers={**PRIVATE_HEADERS, "Cache-Control": "no-cache"})

    @router.api_route("/{slug}", methods=["GET", "HEAD"])
    def artifact_shell(slug: str):
        return shell()

    api = APIRouter(prefix="/api")
    api.add_api_route("/artifacts", ops.list_artifacts, methods=["GET"])
    api.add_api_route("/profiles", ops.profiles, methods=["GET"])
    api.add_api_route("/slugs/{slug}", ops.get_artifact_by_slug, methods=["GET"])
    api.add_api_route("/artifacts/{artifact_id}", ops.get_artifact, methods=["GET"])
    api.add_api_route("/artifacts/{artifact_id}/download", ops.download_artifact, methods=["GET"])
    api.add_api_route("/shares/{share_id}", ops.revoke_share, methods=["DELETE"])

    @api.get("/artifacts/{artifact_id}/frame")
    def frame(artifact_id: str, theme: str = Query("light", pattern="^(light|dark)$")):
        return ops.wrapped_preview(artifact_id, theme)

    @api.post("/artifacts/{artifact_id}/shares", status_code=201)
    async def share(artifact_id: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        seconds = body.get("expires_in_seconds") if isinstance(body, dict) else None
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            return JSONResponse({"error": {"code": "invalid_request",
                                           "message": "choose an expiry: expires_in_seconds must be an integer"}}, 422)
        return ops.create_share(artifact_id, ops.ShareRequest(expires_in_seconds=seconds))

    router.include_router(api)
    return router
