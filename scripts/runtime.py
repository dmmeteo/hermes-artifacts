"""Isolated local runtime: artifact service + Hermes dashboard on loopback ports.

Everything lives under one runtime directory with owner-only permissions. The
Hermes checkout is used read-only; the plugin is copied into an isolated
HERMES_HOME exactly as an operator would install it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HERMES_CHECKOUT = Path(os.environ.get("HERMES_CHECKOUT", Path.home() / ".hermes" / "hermes-agent")).expanduser()
HERMES_PYTHON = HERMES_CHECKOUT / "venv" / "bin" / "python"
SERVICE_PYTHON = PROJECT / ".venv" / "bin" / "python"
SCRATCH = Path(os.environ.get("ARTIFACTS_SCRATCH", PROJECT / ".scratch")).expanduser()


LIVE_ROOTS = [Path.home(), Path.home() / ".hermes", Path.home() / ".hermes" / "profiles",
              Path.home() / ".hermes" / "hermes-agent", Path("/")]


def refuse_live(path: Path) -> Path:
    """Owned scratch only: never a live Hermes home, a profile root, the Hermes checkout or a data root."""
    resolved = path.expanduser().resolve()
    profiles = Path.home() / ".hermes" / "profiles"
    if (resolved in [root.resolve() for root in LIVE_ROOTS] or resolved.parent == profiles.resolve()
            or resolved.name in ("data", "skills", "plugins") or (resolved / "config.yaml").exists()
            or (resolved / "state.db").exists()):
        raise SystemExit(f"refusing to use {resolved}: it is, or looks like, a live home or data root")
    return resolved


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last = exc
        except Exception as exc:  # noqa: BLE001 - polling until the deadline
            last = exc
        time.sleep(0.25)
    raise TimeoutError(f"{url} did not become ready: {last}")


def host_status() -> str:
    return subprocess.run(["git", "-C", str(HERMES_CHECKOUT), "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout


def host_head() -> str:
    return subprocess.run(["git", "-C", str(HERMES_CHECKOUT), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


@dataclass
class Process:
    name: str
    popen: subprocess.Popen
    log: Path

    def stop(self) -> None:
        if self.popen.poll() is None:
            os.killpg(self.popen.pid, signal.SIGTERM)
            try:
                self.popen.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.popen.pid, signal.SIGKILL)
                self.popen.wait(5)


@dataclass
class Runtime:
    root: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    service_port: int = field(default_factory=free_port)
    dashboard_port: int = field(default_factory=free_port)
    processes: dict[str, Process] = field(default_factory=dict)

    @property
    def service_url(self) -> str:
        return f"http://127.0.0.1:{self.service_port}"

    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.dashboard_port}"

    @property
    def hermes_home(self) -> Path:
        return self.root / "hermes-home"

    @property
    def database(self) -> Path:
        return self.root / "service" / "artifacts.db"

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        (self.root / "logs").mkdir(exist_ok=True)
        token_file = self.root / "service" / "api-token"
        token_file.parent.mkdir(exist_ok=True)
        token_file.write_text(self.token)
        os.chmod(token_file, 0o600)
        install_plugin(self.hermes_home, self.service_url, self.token)
        (self.root / "runtime.json").write_text(json.dumps({
            "service_url": self.service_url, "dashboard_url": self.dashboard_url,
            "hermes_home": str(self.hermes_home), "database": str(self.database),
        }, indent=2))

    def _spawn(self, name: str, argv: list[str], env: dict[str, str], cwd: Path) -> Process:
        log = self.root / "logs" / f"{name}.log"
        handle = log.open("ab")
        popen = subprocess.Popen(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        process = Process(name, popen, log)
        self.processes[name] = process
        return process

    def start_service(self, database: Path | None = None, port: int | None = None, name: str = "service",
                      public_url: str | None = None, extra_env: dict[str, str] | None = None) -> str:
        port = port or self.service_port
        env = {
            "PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
            "ARTIFACTS_DB": str(database or self.database),
            "ARTIFACTS_API_TOKEN_FILE": str(self.root / "service" / "api-token"),
            "ARTIFACTS_PUBLIC_URL": public_url or f"http://127.0.0.1:{port}",
            "ARTIFACTS_HOST": "127.0.0.1", "ARTIFACTS_PORT": str(port), **(extra_env or {}),
        }
        self._spawn(name, [str(SERVICE_PYTHON), "-m", "app"], env, PROJECT)
        url = f"http://127.0.0.1:{port}"
        wait_http(f"{url}/healthz", 30)
        return url

    def start_dashboard(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "ARTIFACTS_"))}
        env["HERMES_HOME"] = str(self.hermes_home)
        argv = [str(HERMES_PYTHON), "-m", "hermes_cli.main", "dashboard", "--isolated", "--skip-build",
                "--no-open", "--host", "127.0.0.1", "--port", str(self.dashboard_port)]
        self._spawn("dashboard", argv, env, HERMES_CHECKOUT)
        wait_http(f"{self.dashboard_url}/api/dashboard/plugins", 120)

    def stop(self, name: str) -> None:
        process = self.processes.pop(name, None)
        if process:
            process.stop()

    def stop_all(self) -> None:
        for name in list(self.processes):
            self.stop(name)


def install_plugin(hermes_home: Path, service_url: str, token: str) -> Path:
    """Install the plugin the supported way: a directory under <HERMES_HOME>/plugins plus enablement."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    os.chmod(hermes_home, 0o700)
    target = hermes_home / "plugins" / "artifacts"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PROJECT / "plugin", target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = hermes_home / "config.yaml"
    if not config.exists():
        config.write_text("plugins:\n  enabled:\n    - artifacts\n")
    env_file = hermes_home / ".env"
    env_file.write_text(f"HERMES_ARTIFACTS_URL={service_url}\nHERMES_ARTIFACTS_TOKEN={token}\n")
    os.chmod(env_file, 0o600)
    return target
