from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DAY = 86_400
BCRYPT_RE = re.compile(r"\$2[aby]\$(\d\d)\$[./A-Za-z0-9]{53}")
MIN_BCRYPT_COST = 10
MAX_BCRYPT_COST = 15
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConsoleSettings:
    """The optional standalone console: one operator login, checked against a bcrypt hash."""

    origin: str
    username: str
    password_hash: bytes = b""

    def __repr__(self) -> str:
        return f"ConsoleSettings(origin={self.origin!r}, username={self.username!r})"

    @property
    def host(self) -> str:
        return urlsplit(self.origin).netloc.lower()


@dataclass(frozen=True, slots=True)
class Settings:
    database: Path
    api_token: str
    public_url: str
    max_bytes: int = 10 * 1024 * 1024
    default_share_seconds: int = 7 * DAY
    max_share_seconds: int = 365 * DAY
    public_requests_per_minute: int = 120
    console: ConsoleSettings | None = None

    def __repr__(self) -> str:
        return f"Settings(database={self.database!r}, public_url={self.public_url!r}, console={self.console!r})"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        token = env.get("ARTIFACTS_API_TOKEN", "")
        token_file = env.get("ARTIFACTS_API_TOKEN_FILE")
        if token_file:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ConfigError("ARTIFACTS_API_TOKEN (or _FILE) must hold at least 32 characters")
        return cls(
            database=Path(env.get("ARTIFACTS_DB", "data/artifacts.db")),
            api_token=token,
            public_url=env.get("ARTIFACTS_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"),
            max_bytes=int(env.get("ARTIFACTS_MAX_BYTES", 10 * 1024 * 1024)),
            default_share_seconds=int(env.get("ARTIFACTS_DEFAULT_SHARE_SECONDS", 7 * DAY)),
            max_share_seconds=int(env.get("ARTIFACTS_MAX_SHARE_SECONDS", 365 * DAY)),
            public_requests_per_minute=int(env.get("ARTIFACTS_PUBLIC_RPM", 120)),
            console=console_from_env(env),
        )


def console_from_env(env: dict[str, str]) -> ConsoleSettings | None:
    """Embed-only unless ARTIFACTS_CONSOLE_ORIGIN is set; then a valid login is mandatory."""
    origin = env.get("ARTIFACTS_CONSOLE_ORIGIN", "").strip()
    user = env.get("ARTIFACTS_CONSOLE_USER", "")
    hashed = env.get("ARTIFACTS_CONSOLE_PASSWORD_HASH", "")
    htpasswd = env.get("ARTIFACTS_CONSOLE_HTPASSWD_FILE", "")
    if "ARTIFACTS_CONSOLE_PASSWORD" in env:
        raise ConfigError("ARTIFACTS_CONSOLE_PASSWORD is refused: store a bcrypt hash in "
                          "ARTIFACTS_CONSOLE_PASSWORD_HASH or ARTIFACTS_CONSOLE_HTPASSWD_FILE")
    if not origin:
        if user or hashed or htpasswd:
            raise ConfigError("console credentials are set but ARTIFACTS_CONSOLE_ORIGIN is not")
        return None
    origin = _console_origin(origin)
    if htpasswd and (user or hashed):
        raise ConfigError("set either ARTIFACTS_CONSOLE_HTPASSWD_FILE or ARTIFACTS_CONSOLE_USER with "
                          "ARTIFACTS_CONSOLE_PASSWORD_HASH, not both")
    if htpasswd:
        user, hashed = _htpasswd_entry(Path(htpasswd))
    if not user or not hashed:
        raise ConfigError("the standalone console needs ARTIFACTS_CONSOLE_USER and ARTIFACTS_CONSOLE_PASSWORD_HASH "
                          "(or ARTIFACTS_CONSOLE_HTPASSWD_FILE); there is no unauthenticated console")
    return ConsoleSettings(origin=origin, username=_username(user), password_hash=_bcrypt_hash(hashed))


def _console_origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("ARTIFACTS_CONSOLE_ORIGIN must be an http(s) origin such as https://artifacts.example.com")
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("ARTIFACTS_CONSOLE_ORIGIN must be a bare origin: no credentials, path, query or fragment")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigError("ARTIFACTS_CONSOLE_ORIGIN must use https unless it is a loopback address; "
                          "Basic credentials must not cross a network in clear text")
    netloc = parts.netloc.lower()
    default = {"http": ":80", "https": ":443"}[parts.scheme]
    return f"{parts.scheme}://{netloc.removesuffix(default)}"


def _htpasswd_entry(path: Path) -> tuple[str, str]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeDecodeError):
        raise ConfigError("ARTIFACTS_CONSOLE_HTPASSWD_FILE cannot be read as UTF-8 text") from None
    entries = [line for line in lines if line and not line.startswith("#")]
    if len(entries) != 1 or ":" not in entries[0]:
        raise ConfigError("ARTIFACTS_CONSOLE_HTPASSWD_FILE must hold exactly one user:bcrypt-hash line")
    user, _, hashed = entries[0].partition(":")
    return user, hashed


def _username(value: str) -> str:
    if not 1 <= len(value) <= 64 or ":" in value or not value.isprintable() or value != value.strip():
        raise ConfigError("ARTIFACTS_CONSOLE_USER must be 1-64 printable characters without ':' or edge spaces")
    return value


def _bcrypt_hash(value: str) -> bytes:
    """Only bcrypt (htpasswd -B) is accepted; the value is never echoed into errors or logs."""
    match = BCRYPT_RE.fullmatch(value.strip())
    if not match:
        raise ConfigError("the console password hash is not a bcrypt hash ($2y$/$2b$/$2a$, 60 characters); "
                          "plaintext, MD5, SHA and crypt hashes are refused")
    cost = int(match.group(1))
    if not MIN_BCRYPT_COST <= cost <= MAX_BCRYPT_COST:
        raise ConfigError(f"the console password hash uses bcrypt cost {cost}; use {MIN_BCRYPT_COST}-"
                          f"{MAX_BCRYPT_COST} (htpasswd -nBC 12 USER)")
    return value.strip().encode("ascii")
