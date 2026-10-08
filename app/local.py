"""Try the service locally in one command: ``uv run --frozen python -m app.local``.

The first start creates a private state folder (git-ignored ``.local/`` by default, mode 0700) holding
a random machine credential and a bcrypt console login, both mode 0600. In a terminal the console
password is generated and shown once; otherwise one line is read from stdin. Only the hash is kept.
Later starts reuse the folder as it is; ``--new-password`` replaces only the login. Incomplete or
unsafe state, or a busy port, stops the launcher before it changes anything. The service listens on
loopback only and stops with Ctrl-C.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

import bcrypt

from .__main__ import serve
from .config import ConfigError, Settings

HOST = "127.0.0.1"
USER = "operator"
FILES = ("api-token", "htpasswd")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / ".local"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.local", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080, help="loopback port (default 8080)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="state folder (default: .local in the checkout)")
    parser.add_argument("--new-password", action="store_true", help="replace the console password, keep all data")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1-65535")
    state, origin = args.dir.absolute(), f"http://{HOST}:{args.port}"
    os.umask(0o077)

    first = not os.path.lexists(state)
    if not first:
        check_state(state)
    if not port_free(args.port):
        fail(f"port {args.port} on {HOST} is already in use; stop that process or pass --port")

    shown = None
    if first or args.new_password:
        password, shown = read_password()
        login = f"{USER}:{bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()}\n"
        if first:
            initialize(state, login)
        else:
            replace(state / "htpasswd", login)

    try:
        settings = Settings.from_env({
            "ARTIFACTS_API_TOKEN_FILE": str(state / "api-token"),
            "ARTIFACTS_DB": str(state / "artifacts.db"),
            "ARTIFACTS_PUBLIC_URL": origin,
            "ARTIFACTS_CONSOLE_ORIGIN": origin,
            "ARTIFACTS_CONSOLE_HTPASSWD_FILE": str(state / "htpasswd"),
        })
    except (ConfigError, OSError) as error:
        fail(f"{state} holds unusable settings: {error}")

    secret = (f"{shown}\n            Shown once and not stored. Save it now; --new-password replaces it."
              if shown else "the one you chose or saw at first start (--new-password replaces it)")
    print(f"\nHermes Artifacts (local)\n"
          f"  Console:  {origin}/console/\n"
          f"  Login:    {USER}\n"
          f"  Password: {secret}\n"
          f"  Agents:   machine API at {origin}/internal/v1, bearer token in {state / 'api-token'}\n"
          f"  Data:     {state} (delete it to start over)\n"
          f"Stop with Ctrl-C.\n", flush=True)
    serve(settings, HOST, args.port)
    return 0


def fail(message: str) -> NoReturn:
    raise SystemExit(f"hermes-artifacts: {message}")


def check_state(state: Path) -> None:
    """Refuse, without touching anything, state that is not exactly what a first start creates."""
    info = os.lstat(state)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail(f"{state} must be a directory you own with mode 0700; nothing was changed")
    missing = [name for name in FILES if not os.path.lexists(state / name)]
    if missing:
        fail(f"{state} is incomplete (missing {', '.join(missing)}); nothing was changed. "
             f"Move the folder aside to start over; it may hold your library")
    for name in FILES:
        info = os.lstat(state / name)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail(f"{state / name} must be a regular file you own with mode 0600; nothing was changed")


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def read_password() -> tuple[str, str | None]:
    """Generate and show one in a terminal; otherwise prompt without echo or read a line from stdin."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        password = secrets.token_urlsafe(18)
        return password, password
    password = getpass.getpass("Console password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if not 12 <= len(password.encode()) <= 72:
        fail("the console password must be 12-72 bytes")
    return password, None


def initialize(state: Path, login: str) -> None:
    """Build the folder beside its final path and rename it in, so an interrupted start leaves no state."""
    try:
        staging = Path(tempfile.mkdtemp(prefix=".local-init-", dir=state.parent))
    except OSError as error:
        fail(f"cannot create {state}: {error}")
    try:
        write_new(staging / "api-token", secrets.token_urlsafe(32) + "\n")
        write_new(staging / "htpasswd", login)
        staging.rename(state)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        fail(f"cannot create {state}: {error}")


def replace(path: Path, text: str) -> None:
    staged = path.with_name(path.name + ".new")
    try:
        write_new(staged, text)
        staged.replace(path)
    except OSError as error:
        fail(f"cannot replace {path} (remove a leftover {staged.name} if present): {error}")


def write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    raise SystemExit(main())
