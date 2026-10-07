"""Agent tools: publish privately, browse, read back, share explicitly, revoke."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .client import ArtifactService, ServiceError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TEXT_PREVIEW_CHARS = 4000
EXTENSION_KINDS = {
    ".html": "html", ".htm": "html", ".md": "markdown", ".markdown": "markdown",
    ".svg": "svg", ".png": "png", ".webp": "webp",
}
TEXT_KINDS = {"html", "markdown", "svg"}
_SESSION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def service_available() -> bool:
    return ArtifactService.from_env() is not None


def _profile() -> str:
    try:
        from hermes_cli.profiles import current_profile_name

        name = current_profile_name("default") or "default"
    except Exception:
        name = "default"
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) else "default"


def _dashboard_path(slug: str) -> str:
    return f"/artifacts/{slug}"


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fail(message: str, code: str = "invalid_arguments") -> str:
    return json.dumps({"error": {"code": code, "message": message}})


def _tool(fn: Callable[[ArtifactService, dict, dict], dict]) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        service = ArtifactService.from_env()
        if service is None:
            return _fail("Hermes Artifacts is not configured (HERMES_ARTIFACTS_URL / HERMES_ARTIFACTS_TOKEN).",
                         "not_configured")
        try:
            return _ok(fn(service, args or {}, kwargs))
        except ServiceError as exc:
            return json.dumps(exc.to_json())
        except ValueError as exc:
            return _fail(str(exc))

    handler.__name__ = fn.__name__
    return handler


def _read_source(args: dict) -> tuple[bytes, str]:
    path, content = args.get("path"), args.get("content")
    if bool(path) == (content is not None):
        raise ValueError("provide exactly one of 'path' or 'content'")
    kind = (args.get("kind") or "").strip().lower()
    if path:
        file = Path(os.path.expanduser(str(path)))
        if not file.is_file():
            raise ValueError(f"file not found: {file}")
        if file.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"file exceeds the {MAX_UPLOAD_BYTES} byte limit")
        kind = kind or EXTENSION_KINDS.get(file.suffix.lower(), "")
        if not kind:
            raise ValueError("cannot infer kind from the file extension; pass kind")
        return file.read_bytes(), kind
    if kind not in TEXT_KINDS:
        raise ValueError("inline content needs kind html, markdown or svg; publish images by path")
    return str(content).encode("utf-8"), kind


@_tool
def publish(service: ArtifactService, args: dict, context: dict) -> dict:
    data, kind = _read_source(args)
    title = str(args.get("title") or "").strip()
    if not title and args.get("path"):
        title = Path(str(args["path"])).stem
    session = str(context.get("session_id") or context.get("task_id") or "") or None
    if session and not _SESSION_RE.fullmatch(session):
        session = None
    artifact = service.publish(title=title, kind=kind, content=data, source_profile=_profile(),
                               source_session_id=session)
    return {
        "artifact": artifact,
        "visibility": "private",
        "dashboard_path": _dashboard_path(artifact["slug"]),
        "note": "Saved privately. No guest link exists. Call artifact_share only if the user asks to share it.",
    }


@_tool
def list_artifacts(service: ArtifactService, args: dict, context: dict) -> dict:
    limit = int(args.get("limit") or 20)
    return service.list(q=args.get("query"), kind=args.get("kind"), profile=args.get("profile"),
                        limit=max(1, min(limit, 100)), cursor=args.get("cursor"))


@_tool
def get_artifact(service: ArtifactService, args: dict, context: dict) -> dict:
    artifact_id = str(args.get("artifact_id") or "")
    detail = service.get(artifact_id)
    detail["dashboard_path"] = _dashboard_path(detail["artifact"]["slug"])
    if detail["artifact"]["kind"] in TEXT_KINDS:
        text = service.download(artifact_id).body.decode("utf-8", errors="replace")
        detail["text_preview"] = text[:TEXT_PREVIEW_CHARS]
        detail["text_preview_truncated"] = len(text) > TEXT_PREVIEW_CHARS
    return detail


@_tool
def download(service: ArtifactService, args: dict, context: dict) -> dict:
    target = Path(os.path.expanduser(str(args.get("path") or "")))
    if not str(args.get("path") or "").strip():
        raise ValueError("path is required")
    if target.exists() and not args.get("overwrite"):
        raise ValueError(f"{target} exists; pass overwrite=true to replace it")
    reply = service.download(str(args.get("artifact_id") or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(reply.body)
    return {"path": str(target), "byte_length": len(reply.body),
            "sha256": hashlib.sha256(reply.body).hexdigest()}


@_tool
def share(service: ArtifactService, args: dict, context: dict) -> dict:
    hours = args.get("expires_in_hours")
    seconds: Optional[int] = None if hours in (None, "") else int(float(hours) * 3600)
    grant = service.share(str(args.get("artifact_id") or ""), seconds)
    return {
        **grant,
        "note": "Anyone with this URL can view this one artifact until it expires or is revoked. "
                "The URL is shown only now; keep share.id to revoke it.",
    }


@_tool
def revoke(service: ArtifactService, args: dict, context: dict) -> dict:
    return service.revoke(str(args.get("share_id") or ""))


GUIDE = "Rules for publish vs share, addresses and revocation: skill_view('artifacts:operating')."
_ID = {"type": "string", "description": "Artifact UUID returned by artifact_publish or artifact_list."}

TOOLS = [
    ("artifact_publish", publish, "📦", {
        "name": "artifact_publish",
        "description": "Save a finished document to the shared, private Hermes Artifacts library. Supports "
                       "self-contained HTML (inline CSS/JS only, no external URLs), Markdown, SVG, PNG and WebP. "
                       "Publishing is private and creates no guest link. " + GUIDE,
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of a local file to publish."},
            "content": {"type": "string", "description": "Inline text content (html, markdown or svg)."},
            "kind": {"type": "string", "enum": ["html", "markdown", "svg", "png", "webp"],
                     "description": "Format. Inferred from the file extension when publishing by path."},
            "title": {"type": "string", "description": "Human-readable title (max 200 characters)."},
        }, "required": ["title"]},
    }),
    ("artifact_list", list_artifacts, "🗂️", {
        "name": "artifact_list",
        "description": "List artifacts in the private library, newest first, with optional filters and pagination.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Case-insensitive title search."},
            "kind": {"type": "string", "enum": ["html", "markdown", "svg", "png", "webp"]},
            "profile": {"type": "string", "description": "Only artifacts attributed to this source profile (a label, not an owner)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string", "description": "next_cursor from a previous call."},
        }},
    }),
    ("artifact_get", get_artifact, "🔎", {
        "name": "artifact_get",
        "description": "Read one artifact's metadata, its shares, and a text preview for HTML, Markdown and SVG.",
        "parameters": {"type": "object", "properties": {"artifact_id": _ID}, "required": ["artifact_id"]},
    }),
    ("artifact_download", download, "⬇️", {
        "name": "artifact_download",
        "description": "Write an artifact's original bytes to a local file.",
        "parameters": {"type": "object", "properties": {
            "artifact_id": _ID,
            "path": {"type": "string", "description": "Destination file path."},
            "overwrite": {"type": "boolean", "description": "Replace an existing file."},
        }, "required": ["artifact_id", "path"]},
    }),
    ("artifact_share", share, "🔗", {
        "name": "artifact_share",
        "description": "Create a guest link for exactly one artifact. Only do this when the user explicitly asks "
                       "to share. Links expire (default 7 days) and can be revoked. " + GUIDE,
        "parameters": {"type": "object", "properties": {
            "artifact_id": _ID,
            "expires_in_hours": {"type": "number", "minimum": 0.0003,
                                 "description": "Lifetime in hours. Default 168 (7 days), maximum 8760."},
        }, "required": ["artifact_id"]},
    }),
    ("artifact_revoke_share", revoke, "⛔", {
        "name": "artifact_revoke_share",
        "description": "Revoke one guest link by share id. Other links to the same artifact keep working.",
        "parameters": {"type": "object", "properties": {
            "share_id": {"type": "string", "description": "share.id returned by artifact_share or artifact_get."},
        }, "required": ["share_id"]},
    }),
]
