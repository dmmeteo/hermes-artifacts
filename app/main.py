"""HTTP transport: authenticated management API and the public, read-only share viewer.

Guards live here: credential checks, size and rate limits, path validation and
uniform not-found responses. Domain rules stay in domain.py and store.py.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from types import SimpleNamespace
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .console import ConsoleGuard, console_router
from .domain import (
    KINDS,
    PROFILE_RE,
    SESSION_RE,
    SHARE_TOKEN_RE,
    SLUG_RE,
    Artifact,
    InvalidArtifact,
    clean_title,
    parse_kind,
    validate_payload,
)
from .render import CONTENT_CSP, CONTENT_RESPONSE_HEADERS, Theme, render_document, wrapper_document
from .store import Store

log = logging.getLogger("hermes_artifacts")

SHELL_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
PUBLIC_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}
_VIEWER = files("app") / "viewer"
_PLACEHOLDER = re.compile(r"\{\{(title|kind|expires_at|token)\}\}")
_UNAVAILABLE_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width,initial-scale=1">'
    '<link rel=stylesheet href="/_viewer/viewer.css"><title>Link unavailable</title></head>'
    '<body class="notice"><main><p class="eyebrow">Hermes Artifacts</p><h1>This link is unavailable</h1>'
    "<p>It may have expired, been revoked, or never existed. Ask the person who sent it for a new link.</p>"
    "</main></body></html>"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class ShareRequest(BaseModel):
    expires_in_seconds: int | None = None


class RateLimiter:
    """Per-client token bucket with a bounded client table."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic, max_clients: int = 10_000):
        self.capacity = float(per_minute)
        self.refill = per_minute / 60.0
        self.clock = clock
        self.max_clients = max_clients
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, client: str) -> bool:
        client = client_key(client)
        now = self.clock()
        tokens, last = self.buckets.pop(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill)
        allowed = tokens >= 1
        self.buckets[client] = (tokens - 1 if allowed else tokens, now)
        while len(self.buckets) > self.max_clients:
            self.buckets.popitem(last=False)
        return allowed


def client_key(host: str) -> str:
    """IPv6 clients control a whole /64, so they share one bucket."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def bearer_matches(authorization: str, expected: str) -> bool:
    scheme, _, supplied = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied.encode(), expected.encode())


def declared_length(value: str | None) -> int | None:
    return int(value) if value and value.isascii() and value.isdigit() else None


class InternalGuard:
    """Reject unauthenticated or oversized management requests before FastAPI reads any body."""

    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/internal/"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if not bearer_matches(headers.get("authorization", ""), self.settings.api_token):
                return await _send_json(send, 401, "unauthorized", "a valid service credential is required")
            length = declared_length(headers.get("content-length"))
            if length is not None and length > self.settings.max_bytes:
                return await _send_json(send, 413, "too_large",
                                        f"content exceeds the {self.settings.max_bytes} byte limit")
            streamed_publish = scope["method"] == "POST" and scope["path"] == "/internal/v1/artifacts"
            if "transfer-encoding" in headers and not streamed_publish:
                # Only publish reads its body in capped chunks; every other route needs a declared length.
                return await _send_json(send, 411, "length_required", "send the request body with a Content-Length")
        return await self.app(scope, receive, send)


async def _send_json(send, status: int, code: str, message: str) -> None:
    response = JSONResponse({"error": {"code": code, "message": message}}, status)
    await send({"type": "http.response.start", "status": status, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


_KNOWN_PREFIXES = ("/internal/", "/_viewer/", "/healthz", "/console/")


def redact_path(path: str) -> str:
    """Capability tokens must never reach logs; every public share path collapses to one label."""
    if path in ("/", "/console") or path.startswith(_KNOWN_PREFIXES):
        return re.sub(r"/[A-Za-z0-9_-]{22}(?=/|$)", "/<token>", path)
    return "/<share>"


class AccessLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            log.info(
                "%s %s %s %.1fms", scope["method"], redact_path(scope["path"]), status,
                (time.perf_counter() - started) * 1000,
            )


def create_app(settings: Settings, clock: Callable[[], int] | None = None) -> FastAPI:
    now = clock or (lambda: int(time.time()))
    store = Store(settings.database, now)
    limiter = RateLimiter(settings.public_requests_per_minute)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="Hermes Artifacts")

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": f"{where}: {first.get('msg', 'invalid')}"}}, 422
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"error": {"code": code, "message": str(exc.detail)}}, exc.status_code)

    def require_operator(request: Request) -> None:
        if not bearer_matches(request.headers.get("authorization", ""), settings.api_token):
            raise ApiError(401, "unauthorized", "a valid service credential is required")

    def artifact_or_404(artifact_id: str) -> Artifact:
        try:
            canonical = str(uuid.UUID(artifact_id))
        except ValueError:
            raise ApiError(404, "not_found", "artifact not found") from None
        artifact = store.get(canonical)
        if artifact is None:
            raise ApiError(404, "not_found", "artifact not found")
        return artifact

    def artifact_json(artifact: Artifact, active_shares: int | None = None) -> dict:
        data = artifact.to_json()
        if active_shares is not None:
            data["active_shares"] = active_shares
        return data

    internal = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_operator)])

    @internal.post("/artifacts", status_code=201)
    async def publish(
        request: Request,
        title: Annotated[str, Query(max_length=400)],
        kind: Annotated[str, Query(max_length=20)],
        source_profile: Annotated[str, Query(max_length=64)] = "unknown",
        source_session_id: Annotated[str | None, Query(max_length=128)] = None,
    ):
        if not PROFILE_RE.fullmatch(source_profile):
            raise ApiError(422, "invalid_artifact", "source_profile has an invalid format")
        if source_session_id is not None and not SESSION_RE.fullmatch(source_session_id):
            raise ApiError(422, "invalid_artifact", "source_session_id has an invalid format")
        declared = declared_length(request.headers.get("content-length"))
        if declared is not None and declared > settings.max_bytes:
            raise ApiError(413, "too_large", f"content exceeds the {settings.max_bytes} byte limit")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_bytes:
                raise ApiError(413, "too_large", f"content exceeds the {settings.max_bytes} byte limit")
        try:
            parsed_kind = parse_kind(kind)
            clean = clean_title(title)
            validate_payload(parsed_kind, bytes(body), settings.max_bytes)
        except InvalidArtifact as exc:
            raise ApiError(422, "invalid_artifact", str(exc)) from None
        artifact = await run_in_threadpool(
            store.publish, title=clean, kind=parsed_kind, content=bytes(body),
            source_profile=source_profile, source_session_id=source_session_id,
        )
        return artifact_json(artifact, 0)

    @internal.get("/artifacts")
    def list_artifacts(
        q: Annotated[str | None, Query(max_length=200)] = None,
        kind: Annotated[str | None, Query(max_length=20)] = None,
        profile: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ):
        try:
            parsed_kind = parse_kind(kind) if kind else None
        except InvalidArtifact as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
        try:
            page = store.list(limit=limit, cursor=cursor, query=q or None, kind=parsed_kind,
                              profile=profile or None)
        except ValueError:
            raise ApiError(422, "invalid_request", "cursor is invalid") from None
        counts = store.share_counts([a.id for a in page.items])
        return {
            "items": [artifact_json(a, counts.get(a.id, 0)) for a in page.items],
            "next_cursor": page.next_cursor,
        }

    @internal.get("/profiles")
    def profiles():
        return {"profiles": store.profiles()}

    @internal.get("/slugs/{slug}")
    def get_artifact_by_slug(slug: str):
        """The only slug-to-artifact boundary. Everything after it addresses the artifact by its id."""
        artifact = store.get_by_slug(slug) if SLUG_RE.fullmatch(slug) else None
        if artifact is None:
            raise ApiError(404, "not_found", "artifact not found")
        return get_artifact(artifact.id)

    @internal.get("/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str):
        artifact = artifact_or_404(artifact_id)
        shares = store.shares_for(artifact.id)
        current = now()
        return {
            "artifact": artifact_json(artifact, sum(s.status(current) == "active" for s in shares)),
            "shares": [s.to_json(current) for s in shares],
        }

    @internal.get("/artifacts/{artifact_id}/render")
    def render_artifact(artifact_id: str, theme: Theme = "light"):
        artifact = artifact_or_404(artifact_id)
        document = render_document(artifact, store.content(artifact.id) or b"", theme)
        return HTMLResponse(document, headers={**CONTENT_RESPONSE_HEADERS, "X-Artifact-Frame-Policy": CONTENT_CSP})

    @internal.get("/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str):
        artifact = artifact_or_404(artifact_id)
        return download_response(artifact, store.content(artifact.id) or b"")

    @internal.post("/artifacts/{artifact_id}/shares", status_code=201)
    def create_share(artifact_id: str, request: ShareRequest | None = None):
        artifact = artifact_or_404(artifact_id)
        requested = request.expires_in_seconds if request else None
        seconds = settings.default_share_seconds if requested is None else requested
        if not 1 <= seconds <= settings.max_share_seconds:
            raise ApiError(
                422, "invalid_request",
                f"expires_in_seconds must be between 1 and {settings.max_share_seconds}",
            )
        grant = store.create_share(artifact.id, seconds)
        if grant is None:
            raise ApiError(404, "not_found", "artifact not found")
        return {
            "share": grant.share.to_json(now()),
            "url": f"{settings.public_url}/{grant.token}",
        }

    @internal.get("/artifacts/{artifact_id}/shares")
    def list_shares(artifact_id: str):
        artifact = artifact_or_404(artifact_id)
        current = now()
        return {"shares": [s.to_json(current) for s in store.shares_for(artifact.id)]}

    @internal.delete("/shares/{share_id}")
    def revoke_share(share_id: str):
        try:
            canonical = str(uuid.UUID(share_id))
        except ValueError:
            raise ApiError(404, "not_found", "share not found") from None
        share = store.revoke_share(canonical)
        if share is None:
            raise ApiError(404, "not_found", "share not found")
        return {"share": share.to_json(now())}

    app.include_router(internal)

    def wrapped_preview(artifact_id: str, theme: Theme):
        artifact = artifact_or_404(artifact_id)
        document = render_document(artifact, store.content(artifact.id) or b"", theme)
        return HTMLResponse(wrapper_document(document), headers=CONTENT_RESPONSE_HEADERS)

    if settings.console is not None:
        # Registered before the public /{token} routes so /console/... never reaches the share resolver.
        app.include_router(console_router(SimpleNamespace(
            list_artifacts=list_artifacts, profiles=profiles, get_artifact_by_slug=get_artifact_by_slug,
            get_artifact=get_artifact, download_artifact=download_artifact, create_share=create_share,
            revoke_share=revoke_share, wrapped_preview=wrapped_preview, ShareRequest=ShareRequest,
        )))

    def download_response(artifact: Artifact, content: bytes) -> Response:
        headers = {
            **CONTENT_RESPONSE_HEADERS,
            "Content-Disposition": f'attachment; filename="{artifact.filename()}"',
        }
        return Response(content, media_type=KINDS[artifact.kind].media_type, headers=headers)

    def unavailable() -> HTMLResponse:
        return HTMLResponse(
            _UNAVAILABLE_PAGE, 404, headers={**PUBLIC_HEADERS, "Content-Security-Policy": SHELL_CSP}
        )

    def too_many() -> Response:
        return Response("Too many requests", 429, headers={**PUBLIC_HEADERS, "Retry-After": "30"})

    def public_grant(request: Request, token: str):
        if not limiter.allow(request.client.host if request.client else "unknown"):
            return None, too_many()
        if not SHARE_TOKEN_RE.fullmatch(token):
            return None, unavailable()
        grant = store.resolve_share(token)
        if grant is None:
            return None, unavailable()
        return grant, None

    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.api_route("/_viewer/{asset}", methods=["GET", "HEAD"], include_in_schema=False)
    def viewer_asset(asset: str):
        media = {"viewer.css": "text/css", "viewer.js": "text/javascript"}.get(asset)
        if media is None:
            return unavailable()
        body = (_VIEWER / asset).read_bytes()
        return Response(body, media_type=media, headers={
            "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def root():
        return unavailable()

    @app.api_route("/{token}", methods=["GET", "HEAD"], include_in_schema=False)
    def share_shell(request: Request, token: str):
        grant, failure = public_grant(request, token)
        if failure:
            return failure
        share, artifact = grant
        values = {"title": html.escape(artifact.title), "kind": artifact.kind.value,
                  "expires_at": str(share.expires_at), "token": token}
        page = _PLACEHOLDER.sub(lambda m: values[m[1]], (_VIEWER / "viewer.html").read_text(encoding="utf-8"))
        return HTMLResponse(page, headers={**PUBLIC_HEADERS, "Content-Security-Policy": SHELL_CSP})

    @app.api_route("/{token}/content", methods=["GET", "HEAD"], include_in_schema=False)
    def share_content(request: Request, token: str, theme: str = "light"):
        grant, failure = public_grant(request, token)
        if failure:
            return failure
        theme: Theme = "dark" if theme == "dark" else "light"
        if request.headers.get("sec-fetch-dest") != "iframe":
            # Top-level, the artifact would run with no embedding CSP to contain its navigation.
            return RedirectResponse(f"/{token}", 303, headers=PUBLIC_HEADERS)
        _, artifact = grant
        document = render_document(artifact, store.content(artifact.id) or b"", theme)
        return HTMLResponse(document, headers={**CONTENT_RESPONSE_HEADERS, "X-Robots-Tag": "noindex"})

    @app.api_route("/{token}/download", methods=["GET", "HEAD"], include_in_schema=False)
    def share_download(request: Request, token: str):
        grant, failure = public_grant(request, token)
        if failure:
            return failure
        _, artifact = grant
        return download_response(artifact, store.content(artifact.id) or b"")

    return AccessLog(InternalGuard(ConsoleGuard(app, settings.console, unavailable), settings))
