"""Dashboard adapter: authenticated Hermes routes forwarded to the artifact service.

Hermes mounts ``router`` under /api/plugins/artifacts/ behind its own session
auth. This module is imported by file path without a package, so it loads the
sibling client by path too. Artifact documents travel as JSON so a browser that
navigates here directly never renders artifact markup under the dashboard origin.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, Response


def _load_client():
    name = f"{__name__}_client"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / "client.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_client = _load_client()
router = APIRouter()

_SAFE_DOWNLOAD_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status)


def _call(operation):
    service = _client.ArtifactService.from_env()
    if service is None:
        return _error(503, "not_configured",
                      "Hermes Artifacts is not configured. Set HERMES_ARTIFACTS_URL and HERMES_ARTIFACTS_TOKEN.")
    try:
        return operation(service)
    except _client.ServiceError as exc:
        if exc.status in (401, 403):
            return _error(502, "service_auth_failed", "The artifact service rejected the dashboard credential.")
        return _error(exc.status, exc.code, exc.message)


def _uuid(value: str) -> Optional[str]:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _not_found() -> JSONResponse:
    return _error(404, "not_found", "artifact not found")


@router.get("/status")
def status():
    def check(service):
        service.request("GET", "/profiles")
        return {"configured": True, "reachable": True}

    reply = _call(check)
    if isinstance(reply, JSONResponse) and reply.status_code == 503:
        return {"configured": False, "reachable": False}
    if isinstance(reply, JSONResponse):
        return {"configured": True, "reachable": False}
    return reply


@router.get("/artifacts")
def list_artifacts(
    q: Optional[str] = Query(None, max_length=200),
    kind: Optional[str] = Query(None, max_length=20),
    profile: Optional[str] = Query(None, max_length=64),
    limit: int = Query(30, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=200),
):
    return _call(lambda s: s.list(q=q, kind=kind, profile=profile, limit=limit, cursor=cursor))


@router.get("/profiles")
def profiles():
    return _call(lambda s: s.profiles())


@router.get("/slugs/{slug}")
def get_artifact_by_slug(slug: str):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 64:
        return _not_found()
    return _call(lambda s: s.get_by_slug(slug))


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str):
    canonical = _uuid(artifact_id)
    return _call(lambda s: s.get(canonical)) if canonical else _not_found()


@router.get("/artifacts/{artifact_id}/document")
def document(artifact_id: str, theme: str = Query("light", pattern="^(light|dark)$")):
    canonical = _uuid(artifact_id)
    if not canonical:
        return _not_found()
    return _call(lambda s: JSONResponse(s.render(canonical, theme), headers={"Cache-Control": "no-store"}))


@router.get("/artifacts/{artifact_id}/download")
def download(artifact_id: str):
    canonical = _uuid(artifact_id)
    if not canonical:
        return _not_found()

    def fetch(service):
        reply = service.download(canonical)
        disposition = reply.headers.get("Content-Disposition") or reply.headers.get("content-disposition")
        return Response(reply.body, media_type="application/octet-stream",
                        headers={**_SAFE_DOWNLOAD_HEADERS, "Content-Disposition": disposition or "attachment"})

    return _call(fetch)


@router.post("/artifacts/{artifact_id}/shares")
def create_share(artifact_id: str, body: dict = Body(default_factory=dict)):
    canonical = _uuid(artifact_id)
    if not canonical:
        return _not_found()
    seconds = body.get("expires_in_seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return _error(422, "invalid_request", "choose an expiry: expires_in_seconds must be an integer")
    return _call(lambda s: JSONResponse(s.share(canonical, seconds), 201))


@router.delete("/shares/{share_id}")
def revoke_share(share_id: str):
    canonical = _uuid(share_id)
    if not canonical:
        return _error(404, "not_found", "share not found")
    return _call(lambda s: s.revoke(canonical))
