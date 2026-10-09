"""Bounded real-agent smoke: a fresh Hermes agent publishes and shares through the installed plugin.

The subscription credential is resolved read-only from the operator profile in a
child process and piped to the agent runner's stdin; it is never printed, logged,
or written. Evidence goes to evidence/agent-smoke/report.json with capability
tokens redacted. Usage: python scripts/agent_smoke.py [--max-turns 15]
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import HERMES_CHECKOUT, HERMES_PYTHON, PROJECT, SCRATCH, Runtime, host_status, refuse_live  # noqa: E402

# The Hermes profile whose model and subscription credential the smoke borrows (read-only).
OPERATOR_HOME = Path(os.environ.get("HERMES_SMOKE_PROFILE", Path.home() / ".hermes")).expanduser()
EVIDENCE = SCRATCH / "evidence" / "agent-smoke"
TOKEN_RE = re.compile(r"(?<=/)[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])")
RESOLVE = (
    "import json, sys\n"
    "from hermes_cli.auth_codex import resolve_codex_runtime_credentials\n"
    "c = resolve_codex_runtime_credentials(read_only=True)\n"
    "sys.stdout.write(json.dumps({'api_key': c['api_key'], 'base_url': c['base_url']}))\n"
)


def plugin_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((PROJECT / "plugin").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(PROJECT)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def invoked_tools(tool_calls: list[dict]) -> list[str]:
    """Direct tool names plus the names dispatched through the Tool Search bridge (tool_call.calls)."""
    names = []
    for call in tool_calls:
        names.append(call["name"])
        if call["name"] == "tool_call":
            names += [inner.get("name") for inner in call["arguments"].get("calls", []) if inner.get("name")]
    return names


def reevaluate() -> int:
    """Re-score recorded evidence after an evaluator fix, without another model run."""
    report = json.loads((EVIDENCE / "report.json").read_text())
    invoked = invoked_tools(report["agent"]["tool_calls"])
    rescored = {"agent invoked artifact_publish": invoked.count("artifact_publish") >= 2,
                "agent invoked artifact_share": "artifact_share" in invoked}
    for check in report["checks"]:
        if check["name"] in rescored:
            check["ok"], check["detail"] = rescored[check["name"]], invoked
    report["reevaluated"] = "tool-name extraction fixed for tool_call.calls; scored from recorded evidence, model not re-run"
    report["passed"] = all(c["ok"] for c in report["checks"])
    (EVIDENCE / "report.json").write_text(json.dumps(report, indent=2))
    for c in report["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    return 0 if report["passed"] else 1


def redact(value):
    return json.loads(TOKEN_RE.sub("<share-token>", json.dumps(value)))


def get(url: str, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    refuse_live(SCRATCH)
    if "--reevaluate" in sys.argv:
        return reevaluate()
    max_turns = int(sys.argv[sys.argv.index("--max-turns") + 1]) if "--max-turns" in sys.argv else 15
    if max_turns > 15:
        raise SystemExit("the authorized smoke is bounded to 15 agent turns")
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True)
    root = SCRATCH / "runtime" / "agent-smoke"
    if root.exists():
        shutil.rmtree(root)
    rt = Runtime(root)
    host_before = host_status()
    model = yaml.safe_load((OPERATOR_HOME / "config.yaml").read_text())["model"]["default"]
    checks = []
    report: dict = {"plugin_tree_sha256": plugin_tree_hash(), "model": model, "max_turns": max_turns,
                    "revision": subprocess.run(["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
                                               capture_output=True, text=True).stdout.strip(),
                    "dirty": subprocess.run(["git", "-C", str(PROJECT), "status", "--porcelain"],
                                            capture_output=True, text=True).stdout.strip() != ""}
    try:
        rt.prepare()
        work = root / "agent-work"
        work.mkdir()
        rt.start_service()
        rt.start_dashboard()
        resolved = subprocess.run(
            [str(HERMES_PYTHON), "-c", RESOLVE], cwd=HERMES_CHECKOUT, capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(OPERATOR_HOME)},
        )
        if resolved.returncode != 0:
            report["blocker"] = "read-only credential resolution failed: " + resolved.stderr.strip().splitlines()[-1][:300]
            raise RuntimeError(report["blocker"])
        credentials = json.loads(resolved.stdout)
        credentials["model"] = model
        runner = subprocess.run(
            [str(HERMES_PYTHON), str(PROJECT / "scripts" / "agent_runner.py"), str(work), str(max_turns)],
            cwd=work, input=json.dumps(credentials) + "\n", capture_output=True, text=True, timeout=1200,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(rt.hermes_home)},
        )
        secret = credentials.pop("api_key")
        del credentials
        stderr_clean = runner.stderr.replace(secret, "<redacted>")
        (root / "logs" / "agent-runner.err").write_text(stderr_clean)
        if runner.returncode != 0 or not runner.stdout.strip():
            report["blocker"] = "agent runner failed: " + stderr_clean.strip()[-800:]
            raise RuntimeError(report["blocker"])
        outcome = json.loads(runner.stdout.strip().splitlines()[-1])
        checks.append(("credential never appears in runner output", secret not in runner.stdout, None))
        del secret
        report["agent"] = redact(outcome)
        invoked = invoked_tools(outcome["tool_calls"])
        checks.append(("agent stayed within the turn budget", (outcome.get("api_calls") or 0) <= max_turns,
                       outcome.get("api_calls")))
        checks.append(("agent invoked artifact_publish", invoked.count("artifact_publish") >= 2, invoked))
        checks.append(("agent invoked artifact_share", "artifact_share" in invoked, invoked))

        listing = json.loads(get(f"{rt.service_url}/internal/v1/artifacts?limit=50", rt.token)[1])["items"]
        report["library_after"] = [{k: a[k] for k in ("id", "slug", "title", "kind", "byte_length", "source_profile",
                                                      "source_session_id", "active_shares")} for a in listing]
        html = [a for a in listing if a["kind"] == "html"]
        markdown = [a for a in listing if a["kind"] == "markdown"]
        checks.append(("agent published an HTML and a Markdown artifact", bool(html) and bool(markdown),
                       [a["title"] for a in listing]))
        if html:
            detail = json.loads(get(f"{rt.service_url}/internal/v1/artifacts/{html[0]['id']}", rt.token)[1])
            checks.append(("HTML artifact has exactly one active share",
                           [s["status"] for s in detail["shares"]] == ["active"], detail["shares"]))
            written = work / "tip-calculator.html"
            stored = get(f"{rt.service_url}/internal/v1/artifacts/{html[0]['id']}/download", rt.token)[1]
            checks.append(("stored HTML bytes equal the file the agent wrote",
                           written.exists() and stored == written.read_bytes(), str(written.exists())))
            expiry = detail["shares"][0]["expires_at"] - detail["shares"][0]["created_at"] if detail["shares"] else None
            checks.append(("agent share expires in 24 hours", expiry == 86_400, expiry))
            checks.append(("agent was given the private /artifacts/<slug> path",
                           f"/artifacts/{html[0]['slug']}" in json.dumps(outcome), html[0]["slug"]))
        if markdown:
            detail = json.loads(get(f"{rt.service_url}/internal/v1/artifacts/{markdown[0]['id']}", rt.token)[1])
            checks.append(("Markdown artifact stayed private", detail["shares"] == [], detail["shares"]))
        urls = re.findall(re.escape(rt.service_url) + r"/[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])", json.dumps(outcome))
        if urls:
            status, body = get(urls[0])
            checks.append(("share URL from the agent opens for an anonymous guest", status == 200, status))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(urls[0])
                page.wait_for_timeout(1500)
                page.screenshot(path=str(EVIDENCE / "guest-agent-share.png"))
                page.goto(f"{rt.dashboard_url}/artifacts/{html[0]['slug']}")
                page.wait_for_selector("[data-testid=artifact-frame]", timeout=30_000)
                page.wait_for_timeout(1200)
                page.screenshot(path=str(EVIDENCE / "dashboard-agent-artifact.png"))
                page.goto(f"{rt.dashboard_url}/artifacts")
                page.wait_for_selector(".ha-row", timeout=30_000)
                page.screenshot(path=str(EVIDENCE / "dashboard-agent-library.png"))
                browser.close()
        else:
            checks.append(("agent reported a share URL", False, None))
    except Exception as exc:  # noqa: BLE001 - recorded as a failed gate
        checks.append(("agent smoke completed", False, str(exc)[-800:]))
    finally:
        rt.stop_all()
    checks.append(("Hermes checkout unchanged", host_status() == host_before, None))
    report["checks"] = [{"name": n, "ok": bool(ok), "detail": redact(d)} for n, ok, d in checks]
    report["passed"] = all(c["ok"] for c in report["checks"])
    (EVIDENCE / "report.json").write_text(json.dumps(report, indent=2))
    for c in report["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
