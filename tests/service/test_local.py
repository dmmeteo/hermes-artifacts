"""One-command local launcher: real service on loopback, protected console, state reused on restart."""

import os
import pty
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "synthetic-" + secrets.token_urlsafe(12)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(state: Path, port: int, *args: str, stdin: str | None = PASSWORD) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTIFACTS_")}
    proc = subprocess.Popen([sys.executable, "-m", "app.local", "--dir", str(state), "--port", str(port), *args],
                            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if stdin is not None:
        proc.stdin.write(stdin + "\n")
    proc.stdin.close()
    return proc


def wait_ready(proc: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert proc.poll() is None, proc.stdout.read()
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.1)
    raise AssertionError("service did not start")


def stop(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0, out
    return out


def finish(proc: subprocess.Popen) -> tuple[int, str]:
    out, _ = proc.communicate(timeout=20)
    return proc.returncode, out


def snapshot(state: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mode) for p in state.iterdir()} if state.exists() else {}


@pytest.fixture
def started(tmp_path):
    state, port = tmp_path / "local", free_port()
    proc = launch(state, port)
    wait_ready(proc, port)
    yield state, port, proc
    if proc.poll() is None:
        proc.kill()
        proc.communicate()


def test_first_start_serves_a_protected_console_and_machine_api(started):
    state, port, proc = started
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/console/").status_code == 401
    assert httpx.get(f"{base}/console/", auth=("operator", "wrong-password-123")).status_code == 401
    page = httpx.get(f"{base}/console/", auth=("operator", PASSWORD))
    assert page.status_code == 200 and "<html" in page.text.lower()

    assert httpx.get(f"{base}/internal/v1/artifacts").status_code == 401
    token = (state / "api-token").read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    r = httpx.post(f"{base}/internal/v1/artifacts", params={"title": "Hello", "kind": "markdown"},
                   content=b"# Hello", headers=auth)
    assert r.status_code == 201 and r.json()["active_shares"] == 0
    assert httpx.get(f"{base}/internal/v1/artifacts/{r.json()['id']}/shares", headers=auth).json() == {"shares": []}

    out = stop(proc)
    assert f"{base}/console/" in out
    assert PASSWORD not in out  # a password the user typed is never echoed back


def test_state_is_private_and_holds_no_plaintext_password(started):
    state, _, proc = started
    stop(proc)
    assert state.stat().st_mode & 0o777 == 0o700
    for path in state.iterdir():
        assert path.stat().st_mode & 0o077 == 0, path.name
        assert PASSWORD.encode() not in path.read_bytes(), path.name
    assert re.fullmatch(r"operator:\$2b\$12\$\S{53}\n", (state / "htpasswd").read_text())
    assert len((state / "api-token").read_text().strip()) >= 32


def test_restart_reuses_data_and_credentials(started):
    state, port, proc = started
    base = f"http://127.0.0.1:{port}"
    token = (state / "api-token").read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    created = httpx.post(f"{base}/internal/v1/artifacts", params={"title": "Kept", "kind": "markdown"},
                         content=b"# Kept", headers=auth).json()
    stop(proc)
    before = (state / "api-token").read_bytes(), (state / "htpasswd").read_bytes()

    again = launch(state, port, stdin=None)
    try:
        wait_ready(again, port)
        assert httpx.get(f"{base}/internal/v1/artifacts/{created['id']}", headers=auth).status_code == 200
        assert httpx.get(f"{base}/console/", auth=("operator", PASSWORD)).status_code == 200
    finally:
        out = stop(again)
    assert ((state / "api-token").read_bytes(), (state / "htpasswd").read_bytes()) == before
    assert "first start" in out.lower()


def test_new_password_is_explicit_and_keeps_data(started):
    state, port, proc = started
    base = f"http://127.0.0.1:{port}"
    stop(proc)
    token, db = (state / "api-token").read_bytes(), (state / "artifacts.db").read_bytes()
    replacement = "replacement-" + secrets.token_urlsafe(12)
    proc = launch(state, port, "--new-password", stdin=replacement)
    try:
        wait_ready(proc, port)
        assert httpx.get(f"{base}/console/", auth=("operator", PASSWORD)).status_code == 401
        assert httpx.get(f"{base}/console/", auth=("operator", replacement)).status_code == 200
    finally:
        stop(proc)
    assert (state / "api-token").read_bytes() == token
    assert (state / "artifacts.db").exists() and len(db) > 0
    assert (state / "htpasswd").stat().st_mode & 0o777 == 0o600


def test_generated_password_is_shown_once_in_a_terminal_and_works(tmp_path):
    state, port = tmp_path / "local", free_port()
    leader, follower = pty.openpty()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTIFACTS_")}
    proc = subprocess.Popen([sys.executable, "-m", "app.local", "--dir", str(state), "--port", str(port)],
                            cwd=ROOT, env=env, stdin=follower, stdout=follower, stderr=follower)
    os.close(follower)
    output = b""
    try:
        deadline = time.monotonic() + 20
        while b"/console/" not in output and time.monotonic() < deadline:
            if select.select([leader], [], [], 0.5)[0]:
                output += os.read(leader, 4096)
        match = re.search(rb"Password: (\S+)", output)
        assert match, output
        password = match.group(1).decode()
        assert len(password) >= 20
        wait_ready(proc, port)
        assert httpx.get(f"http://127.0.0.1:{port}/console/", auth=("operator", password)).status_code == 200
        for path in state.iterdir():
            assert password.encode() not in path.read_bytes()
    finally:
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=15) == 0
        os.close(leader)


def test_partial_state_is_refused_untouched(started):
    state, port, proc = started
    stop(proc)
    (state / "htpasswd").unlink()
    before = snapshot(state)
    code, out = finish(launch(state, port))
    assert code != 0 and "incomplete" in out
    assert snapshot(state) == before


def test_unsafe_state_permissions_are_refused_untouched(started):
    state, port, proc = started
    stop(proc)
    (state / "api-token").chmod(0o644)
    before = snapshot(state)
    code, out = finish(launch(state, port))
    assert code != 0 and "api-token" in out and "0600" in out
    assert snapshot(state) == before

    (state / "api-token").chmod(0o600)
    state.chmod(0o755)
    before = snapshot(state)
    code, out = finish(launch(state, port))
    assert code != 0 and "0700" in out
    assert snapshot(state) == before


def test_occupied_port_fails_clearly_without_creating_state(tmp_path):
    state = tmp_path / "local"
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, out = finish(launch(state, port))
    assert code != 0 and f"port {port}" in out
    assert not state.exists()
