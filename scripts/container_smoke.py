"""Build the image and smoke-test it in an isolated, labeled container.

Docker resources carry the label artifacts-build=hermes-artifacts-uat and are
removed afterwards. Prints a JSON report; exits 1 on any failed check.
"""

import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LABEL = "artifacts-build=hermes-artifacts-uat"
NAME = "hermes-artifacts-uat"
FIXTURE = PROJECT / "tests" / "fixtures" / "chart.webp"


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=900)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def http(method, url, token=None, body=None, headers=None):
    request = urllib.request.Request(url, data=body, method=method, headers={
        **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def cleanup() -> None:
    docker("rm", "-f", NAME, check=False)
    docker("volume", "rm", f"{NAME}-data", check=False)
    docker("network", "rm", f"{NAME}-net", check=False)


def main() -> int:
    revision = subprocess.run(["git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    tag = f"hermes-artifacts:uat-{revision}"
    checks = []
    token = secrets.token_urlsafe(32)
    cleanup()
    try:
        docker("build", "-q", "--label", LABEL, "--build-arg", f"REVISION={revision}", "-t", tag, str(PROJECT))
        size = int(docker("image", "inspect", tag, "--format", "{{.Size}}"))
        user = docker("image", "inspect", tag, "--format", "{{.Config.User}}")
        checks.append(("image runs as a non-root user", user == "artifacts", user))
        checks.append(("image stays lean (< 300 MB)", size < 300 * 1024 * 1024, size))
        docker("network", "create", "--label", LABEL, f"{NAME}-net")
        docker("volume", "create", "--label", LABEL, f"{NAME}-data")
        port = 18000 + secrets.randbelow(2000)
        docker("run", "-d", "--name", NAME, "--label", LABEL, "--network", f"{NAME}-net", "--read-only",
               "--tmpfs", "/tmp:size=16m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "-v", f"{NAME}-data:/data", "-p", f"127.0.0.1:{port}:8080",
               "-e", f"ARTIFACTS_API_TOKEN={token}", "-e", f"ARTIFACTS_PUBLIC_URL=http://127.0.0.1:{port}", tag)
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        python = docker("exec", NAME, "python", "-c", "import sys; print(sys.version.split()[0])")
        checks.append(("container runs Python 3.14", python.startswith("3.14"), python))
        status, body = http("POST", f"{base}/internal/v1/artifacts?title=Chart&kind=webp", token,
                            FIXTURE.read_bytes())
        artifact = json.loads(body)
        checks.append(("container publishes an artifact", status == 201, status))
        status, body = http("POST", f"{base}/internal/v1/artifacts/{artifact['id']}/shares", token, b"{}",
                            {"Content-Type": "application/json"})
        share_token = json.loads(body)["url"].rsplit("/", 1)[1]
        docker("restart", NAME)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        status, body = http("GET", f"{base}/{share_token}/download")
        checks.append(("share survives container restart on the volume", status == 200 and body == FIXTURE.read_bytes(),
                       status))
        checks.append(("container management API rejects anonymous calls",
                       http("GET", f"{base}/internal/v1/artifacts")[0] == 401, None))
        checks.append(("container is embed-only unless a console origin is configured",
                       http("GET", f"{base}/console/")[0] == 404, None))
        shipped = docker("exec", NAME, "python", "-c", "import importlib.resources as r;"
                         " print(all((r.files('app') / 'console' / n).is_file() for n in"
                         " ('console.html', 'console.js', 'console.css')))")
        checks.append(("image ships the shared panel for the optional console", shipped == "True", shipped))
        health = docker("inspect", NAME, "--format", "{{json .State.Health.Status}}")
        logs = docker("logs", NAME)
        checks.append(("container logs contain no share token or credential",
                       share_token not in logs and token not in logs, None))
        checks.append(("container healthcheck is configured", health.strip('"') in ("starting", "healthy"), health))
    finally:
        cleanup()
    leftovers = docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}")
    checks.append(("no labeled containers left behind", leftovers == "", leftovers))
    report = {"image": tag, "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks]}
    print(json.dumps(report, indent=2))
    return 0 if all(c["ok"] for c in report["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
