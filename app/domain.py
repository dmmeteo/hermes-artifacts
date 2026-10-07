"""Artifact and Share data shapes plus the rules that make them valid.

An Artifact is an immutable, private payload. A Share is a separate, expiring,
revocable grant to exactly one Artifact. Publishing never creates a Share.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    HTML = "html"
    MARKDOWN = "markdown"
    SVG = "svg"
    PNG = "png"
    WEBP = "webp"


@dataclass(frozen=True, slots=True)
class KindSpec:
    media_type: str
    extension: str
    is_text: bool


KINDS: dict[Kind, KindSpec] = {
    Kind.HTML: KindSpec("text/html; charset=utf-8", "html", True),
    Kind.MARKDOWN: KindSpec("text/markdown; charset=utf-8", "md", True),
    Kind.SVG: KindSpec("image/svg+xml", "svg", True),
    Kind.PNG: KindSpec("image/png", "png", False),
    Kind.WEBP: KindSpec("image/webp", "webp", False),
}


class InvalidArtifact(ValueError):
    """Raised when a payload or its metadata cannot become an Artifact."""


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    slug: str
    title: str
    kind: Kind
    created_at: int
    source_profile: str
    source_session_id: str | None
    content_sha256: str
    byte_length: int

    def filename(self) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", self.title).strip("-.")[:80] or "artifact"
        return f"{stem}.{KINDS[self.kind].extension}"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "kind": self.kind.value,
            "media_type": KINDS[self.kind].media_type,
            "created_at": self.created_at,
            "source_profile": self.source_profile,
            "source_session_id": self.source_session_id,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class Share:
    id: str
    artifact_id: str
    created_at: int
    expires_at: int
    revoked_at: int | None

    def status(self, now: int) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if now >= self.expires_at:
            return "expired"
        return "active"

    def to_json(self, now: int) -> dict:
        return {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "status": self.status(now),
        }


SHARE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")
"""128 random bits as unpadded base64url. The token is the whole grant; only its SHA-256 is stored."""
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""Private library name of an artifact. It identifies, never authorizes: guests need a share token."""
SLUG_MAX = 60
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SESSION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def new_share_token() -> str:
    return secrets.token_urlsafe(16)


def slug_base(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:SLUG_MAX].rstrip("-")
    return base or "artifact"


def slug_candidates(title: str):
    """`orbit-lab`, then `orbit-lab-2`, `orbit-lab-3`, ... ; the store keeps the first free one."""
    base = slug_base(title)
    yield base
    n = 2
    while True:
        suffix = f"-{n}"
        yield base[: SLUG_MAX - len(suffix)].rstrip("-") + suffix
        n += 1


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def clean_title(title: str) -> str:
    cleaned = _CONTROL_RE.sub(" ", title).strip()
    if not cleaned:
        raise InvalidArtifact("title is required")
    if len(cleaned) > 200:
        raise InvalidArtifact("title must be at most 200 characters")
    return cleaned


def parse_kind(value: str) -> Kind:
    try:
        return Kind(value.strip().lower())
    except ValueError:
        allowed = ", ".join(k.value for k in Kind)
        raise InvalidArtifact(f"kind must be one of: {allowed}") from None


def validate_payload(kind: Kind, content: bytes, max_bytes: int) -> None:
    if not content:
        raise InvalidArtifact("content is empty")
    if len(content) > max_bytes:
        raise InvalidArtifact(f"content exceeds the {max_bytes} byte limit")
    if kind is Kind.PNG and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidArtifact("content is not a PNG image")
    if kind is Kind.WEBP and not (content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
        raise InvalidArtifact("content is not a WebP image")
    if KINDS[kind].is_text:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidArtifact(f"{kind.value} content must be UTF-8 text") from None
        if "\x00" in text:
            raise InvalidArtifact(f"{kind.value} content must not contain NUL bytes")
        if kind is Kind.SVG and "<svg" not in text[:4096].lower():
            raise InvalidArtifact("content is not an SVG document")
