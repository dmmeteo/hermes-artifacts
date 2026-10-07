"""Launch, doctor, drive and clean up a disposable Hermes Artifacts stack.

python .claude/skills/verify-hermes-artifacts/scripts/recipe.py STEP [--root DIR] [--checkout DIR]
STEP: launch | doctor | drive | cleanup. --root defaults to $ARTIFACTS_SCRATCH/runtime/recipe; live Hermes
homes and data roots are refused. HERMES_CHECKOUT selects the Hermes source (its venv runs the dashboard).
Secrets are synthetic, live only under --root (0600) and are never printed. Evidence goes to
$ARTIFACTS_SCRATCH/evidence/recipe/<stamp>/ and survives cleanup.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def http(method, url, headers=None, body=None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        return 0, str(exc).encode()


def launch(root: Path, rt_mod) -> int:
    if (root / "state.json").exists():
        print("already launched; run cleanup first", file=sys.stderr)
        return 1
    rt = rt_mod.Runtime(root)
    rt.prepare()
    password = "synthetic-" + secrets.token_urlsafe(18)
    line = subprocess.run([str(rt_mod.SERVICE_PYTHON), "-m", "app.hash_password", "operator"], cwd=rt_mod.PROJECT,
                          input=password + "\n", capture_output=True, text=True, check=True).stdout
    for name, value in (("htpasswd", line), ("console-password", password)):
        (root / "service" / name).write_text(value)
        os.chmod(root / "service" / name, 0o600)
    rt.start_service(extra_env={"ARTIFACTS_CONSOLE_ORIGIN": rt.service_url,
                                "ARTIFACTS_CONSOLE_HTPASSWD_FILE": str(root / "service" / "htpasswd")})
    rt.start_dashboard()
    state = {"service_url": rt.service_url, "dashboard_url": rt.dashboard_url, "hermes_home": str(rt.hermes_home),
             "pids": {name: p.popen.pid for name, p in rt.processes.items()}, "logs": str(root / "logs")}
    (root / "state.json").write_text(json.dumps(state, indent=2))
    print(json.dumps({k: v for k, v in state.items() if k != "pids"}))
    return 0


def doctor(root: Path) -> int:
    state = json.loads((root / "state.json").read_text())
    svc, dash = state["service_url"], state["dashboard_url"]
    plugins = http("GET", f"{dash}/api/dashboard/plugins")
    results = {
        "service healthy": http("GET", f"{svc}/healthz")[0] == 200,
        "console demands a login": http("GET", f"{svc}/console/")[0] == 401,
        "management API demands the machine token": http("GET", f"{svc}/internal/v1/artifacts")[0] == 401,
        "dashboard lists the artifacts plugin": plugins[0] == 200 and b'"artifacts"' in plugins[1],
        "dashboard serves the shared panel": http("GET", f"{dash}/dashboard-plugins/artifacts/dist/console/console.html")[0] == 200,
        "plugin API demands the Hermes session": http("GET", f"{dash}/api/plugins/artifacts/artifacts")[0] == 401,
    }
    print(json.dumps(results, indent=2))
    return 0 if all(results.values()) else 1


def drive(root: Path, rt_mod, scratch: Path) -> int:
    from playwright.sync_api import sync_playwright

    state = json.loads((root / "state.json").read_text())
    svc, dash = state["service_url"], state["dashboard_url"]
    token = (root / "service" / "api-token").read_text()
    password = (root / "service" / "console-password").read_text()
    evidence = scratch / "evidence" / "recipe" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence.mkdir(parents=True)
    results: dict[str, object] = {}
    bearer = {"Authorization": f"Bearer {token}"}

    # Feature: shared library. Two attributions, one library.
    for title, profile in (("Recipe note from prime", "prime"), ("Recipe note from steve", "steve")):
        query = urllib.parse.urlencode({"title": title, "kind": "markdown", "source_profile": profile})
        status, body = http("POST", f"{svc}/internal/v1/artifacts?{query}", bearer, f"# {title}\n".encode())
        results[f"publish {profile}"] = status == 201
    artifact = json.loads(http("GET", f"{svc}/internal/v1/artifacts?limit=100", bearer)[1])["items"][0]

    # Feature: packaged operating skill, discovered by a real Hermes host.
    host = subprocess.run([str(rt_mod.HERMES_PYTHON), str(rt_mod.PROJECT / "scripts" / "host_skill.py"), "Recipe note from host"],
                          cwd=rt_mod.HERMES_CHECKOUT, capture_output=True, text=True, timeout=300,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": state["hermes_home"],
                               "HERMES_PROFILE_NAME": "recipe"})
    skill = json.loads(host.stdout.strip().splitlines()[-1]) if host.returncode == 0 else {}
    results["packaged skill discoverable"] = skill.get("skills_list_has_operating") is True and skill.get("skill_view_success") is True
    results["host sees the shared library"] = "Recipe note from prime" in skill.get("library_titles", [])

    # Feature: guest share, explicit, expiring and revocable, through the console API.
    auth = "Basic " + base64.b64encode(f"operator:{password}".encode()).decode()
    console = {"Authorization": auth, "X-Artifacts-Console": "1", "Origin": svc, "Content-Type": "application/json"}
    status, body = http("POST", f"{svc}/console/api/artifacts/{artifact['id']}/shares", console,
                        json.dumps({"expires_in_seconds": 600}).encode())
    grant = json.loads(body) if status == 201 else {}
    results["share created"] = status == 201
    results["guest opens link"] = http("GET", grant.get("url", svc + "/x"))[0] == 200
    http("DELETE", f"{svc}/console/api/shares/{grant.get('share', {}).get('id', 'x')}", console)
    results["revoked link unavailable"] = http("GET", grant.get("url", svc + "/x"))[0] == 404

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # Feature: standalone console with the configured login.
        context = browser.new_context(viewport={"width": 1280, "height": 800},
                                      http_credentials={"username": "operator", "password": password})
        page = context.new_page()
        page.goto(f"{svc}/console/")
        page.wait_for_selector(".ha-row", timeout=20_000)
        results["standalone rows"] = page.locator(".ha-row").count()
        page.screenshot(path=str(evidence / "standalone-library.png"))
        context.close()
        # Feature: the same panel inside the Hermes dashboard.
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"{dash}/artifacts")
        page.wait_for_load_state("networkidle")
        results["embedded rows"] = 0
        for _ in range(5):  # the dashboard may remount its tab while it settles
            try:
                frame = page.wait_for_selector("[data-testid=artifacts-panel]", timeout=30_000).content_frame()
                frame.wait_for_selector(".ha-row", timeout=30_000)
                results["embedded rows"] = frame.locator(".ha-row").count()
                break
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(500)
        page.screenshot(path=str(evidence / "embedded-library.png"))
        browser.close()
    results["both hosts show the same library"] = results["standalone rows"] == results["embedded rows"] >= 3
    passed = all(v is True for k, v in results.items() if not k.endswith("rows"))
    (evidence / "report.json").write_text(json.dumps({"passed": passed, "results": results}, indent=2))
    print(json.dumps({"passed": passed, "evidence": str(evidence), "results": results}, indent=2))
    return 0 if passed else 1


def _alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cleanup(root: Path) -> int:
    state_file = root / "state.json"
    if state_file.exists():
        pids = list(json.loads(state_file.read_text())["pids"].values())
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        while pids and time.monotonic() < deadline:  # a stopping dashboard still writes into its home
            pids = [pid for pid in pids if _alive(pid)]
            time.sleep(0.2)
        for pid in pids:
            os.killpg(pid, signal.SIGKILL)
    if root.exists():
        shutil.rmtree(root)
    print(json.dumps({"removed": str(root)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["launch", "doctor", "drive", "cleanup"])
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.checkout / "scripts"))
    import runtime as rt_mod

    root = rt_mod.refuse_live(args.root or rt_mod.SCRATCH / "runtime" / "recipe")
    if args.step == "launch":
        return launch(root, rt_mod)
    if args.step == "doctor":
        return doctor(root)
    if args.step == "drive":
        return drive(root, rt_mod, rt_mod.SCRATCH)
    return cleanup(root)


if __name__ == "__main__":
    raise SystemExit(main())
