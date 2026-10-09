"""SQLite persistence for artifacts and shares.

Payload bytes live beside their metadata so publication is one transaction and
an online backup captures a consistent library. Artifacts are immutable; a
trigger rejects updates. Callers pass a clock so expiry is testable.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .domain import Artifact, Kind, Share, hash_share_token, new_share_token, slug_candidates

SCHEMA_VERSION = 2
"""2 added artifact slugs and 22-character share tokens. Older databases are not migrated: start a new one."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    source_profile TEXT NOT NULL,
    source_session_id TEXT,
    content_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    content BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_newest ON artifacts (created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS shares_by_artifact ON shares (artifact_id, created_at DESC);
CREATE TRIGGER IF NOT EXISTS artifacts_immutable BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;
"""

_ARTIFACT_COLUMNS = (
    "id, slug, title, kind, created_at, source_profile, source_session_id, content_sha256, byte_length"
)
_SHARE_COLUMNS = "id, artifact_id, created_at, expires_at, revoked_at"


@dataclass(frozen=True, slots=True)
class Page:
    items: list[Artifact]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ShareGrant:
    share: Share
    token: str


class Store:
    def __init__(self, path: Path, clock: Callable[[], int]):
        self.path = path
        self.clock = clock
        _create_private(path)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            populated = db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'artifacts'").fetchone()[0]
            if version != SCHEMA_VERSION and (populated or version):
                raise RuntimeError(
                    f"{path} has artifacts schema {version}, this service needs {SCHEMA_VERSION}; "
                    "back it up and start with a new database (no migration exists)"
                )
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            yield db

    def publish(
        self,
        *,
        title: str,
        kind: Kind,
        content: bytes,
        source_profile: str,
        source_session_id: str | None,
    ) -> Artifact:
        """Insert under the first free slug for the title; the UNIQUE index settles races."""
        artifact_id, created_at, digest = str(uuid.uuid4()), self.clock(), hashlib.sha256(content).hexdigest()
        with self._connect() as db:
            for slug in slug_candidates(title):
                artifact = Artifact(artifact_id, slug, title, kind, created_at, source_profile, source_session_id,
                                    digest, len(content))
                try:
                    db.execute(
                        f"INSERT INTO artifacts ({_ARTIFACT_COLUMNS}, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (artifact.id, artifact.slug, artifact.title, artifact.kind.value, artifact.created_at,
                         artifact.source_profile, artifact.source_session_id, artifact.content_sha256,
                         artifact.byte_length, content),
                    )
                    return artifact
                except sqlite3.IntegrityError as exc:
                    if "artifacts.slug" not in str(exc):
                        raise
        raise AssertionError("unreachable: slug candidates are unbounded")

    def get_by_slug(self, slug: str) -> Artifact | None:
        with self._connect() as db:
            row = db.execute(f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE slug = ?", (slug,)).fetchone()
        return _artifact(row) if row else None

    def get(self, artifact_id: str) -> Artifact | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return _artifact(row) if row else None

    def content(self, artifact_id: str) -> bytes | None:
        with self._connect() as db:
            row = db.execute("SELECT content FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return bytes(row[0]) if row else None

    def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        query: str | None = None,
        kind: Kind | None = None,
        profile: str | None = None,
    ) -> Page:
        clauses: list[str] = []
        params: list[object] = []
        if cursor:
            created_at, artifact_id = _decode_cursor(cursor)
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params += [created_at, created_at, artifact_id]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("title LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if kind:
            clauses.append("kind = ?")
            params.append(kind.value)
        if profile:
            clauses.append("source_profile = ?")
            params.append(profile)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [_artifact(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(items[-1]) if len(rows) > limit else None
        return Page(items, next_cursor)

    def profiles(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT source_profile FROM artifacts ORDER BY source_profile"
            ).fetchall()
        return [row[0] for row in rows]

    def create_share(self, artifact_id: str, expires_in: int) -> ShareGrant | None:
        token = new_share_token()
        now = self.clock()
        share = Share(
            id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            created_at=now,
            expires_at=now + expires_in,
            revoked_at=None,
        )
        with self._connect() as db:
            try:
                db.execute(
                    f"INSERT INTO shares ({_SHARE_COLUMNS}, token_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (share.id, share.artifact_id, share.created_at, share.expires_at, None,
                     hash_share_token(token)),
                )
            except sqlite3.IntegrityError:
                return None
        return ShareGrant(share, token)

    def shares_for(self, artifact_id: str) -> list[Share]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_SHARE_COLUMNS} FROM shares WHERE artifact_id = ? ORDER BY created_at DESC, id",
                (artifact_id,),
            ).fetchall()
        return [Share(*row) for row in rows]

    def share_counts(self, artifact_ids: list[str]) -> dict[str, int]:
        if not artifact_ids:
            return {}
        marks = ",".join("?" * len(artifact_ids))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT artifact_id, COUNT(*) FROM shares WHERE artifact_id IN ({marks}) "
                "AND revoked_at IS NULL AND expires_at > ? GROUP BY artifact_id",
                (*artifact_ids, self.clock()),
            ).fetchall()
        return dict(rows)

    def revoke_share(self, share_id: str) -> Share | None:
        with self._connect() as db:
            db.execute(
                "UPDATE shares SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self.clock(), share_id),
            )
            row = db.execute(f"SELECT {_SHARE_COLUMNS} FROM shares WHERE id = ?", (share_id,)).fetchone()
        return Share(*row) if row else None

    def resolve_share(self, token: str) -> tuple[Share, Artifact] | None:
        """Return the active grant for a token; None for unknown, expired or revoked."""
        with self._connect() as db:
            row = db.execute(
                f"SELECT s.id, s.artifact_id, s.created_at, s.expires_at, s.revoked_at, "
                f"{', '.join('a.' + c.strip() for c in _ARTIFACT_COLUMNS.split(','))} "
                "FROM shares s JOIN artifacts a ON a.id = s.artifact_id WHERE s.token_hash = ?",
                (hash_share_token(token),),
            ).fetchone()
        if row is None:
            return None
        share = Share(*row[:5])
        if share.status(self.clock()) != "active":
            return None
        return share, _artifact(row[5:])

    def backup(self, destination: Path) -> None:
        backup_database(self.path, destination)


def backup_database(source: Path, destination: Path) -> None:
    """Online copy that opens the source read-only, so it neither upgrades nor refuses any schema.
    Raises FileExistsError rather than overwrite; the copy is owner-only like the library."""
    _create_private(destination, exclusive=True)
    with (closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as db,
          closing(sqlite3.connect(destination)) as target):
        db.backup(target)


def _create_private(path: Path, exclusive: bool = False) -> None:
    """The library holds every artifact, so a new database file (and SQLite's -wal/-shm, which copy its
    mode) is created 0600 in a 0700 folder whatever the process umask. Existing files are left as they are."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        if exclusive:
            raise


def _artifact(row: tuple) -> Artifact:
    return Artifact(row[0], row[1], row[2], Kind(row[3]), row[4], row[5], row[6], row[7], row[8])


def _encode_cursor(artifact: Artifact) -> str:
    raw = f"{artifact.created_at}:{artifact.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, artifact_id = raw.split(":", 1)
        return int(created_at), str(uuid.UUID(artifact_id))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid cursor") from None
