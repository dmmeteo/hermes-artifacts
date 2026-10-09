"""Bounded real-agent smoke: a fresh Hermes agent publishes and shares through the installed plugin.

The subscription credential is resolved read-only from the operator profile in a
child process and piped to the agent runner's stdin; it is never printed, logged,
or written. Evidence goes to evidence/agent-smoke/report.json with capability
tokens redacted. Usage: python scripts/agent_smoke.py [--max-turns 15]

Split placement, when the browser host must not hold the credential: on the browser host set
ARTIFACTS_SMOKE_EXCHANGE=<dir>; the smoke writes <dir>/request.json (service URL and the synthetic
machine token) and waits for <dir>/outcome.json. On the credential host, forward the service port and run
`python scripts/agent_smoke.py --runner <dir>` on a copy of request.json; it writes outcome.json, which
carries the credential-free runner output and the files the agent wrote. Every check still runs.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import (HERMES_CHECKOUT, HERMES_PYTHON, PROJECT, SCRATCH, Runtime, host_status,  # noqa: E402
                     install_plugin, refuse_live)

# The Hermes profile whose model and subscription credential the smoke borrows (read-only).
OPERATOR_HOME = Path(os.environ.get("HERMES_SMOKE_PROFILE", Path.home() / ".hermes")).expanduser()
EVIDENCE = SCRATCH / "evidence" / "agent-smoke"
EXCHANGE = os.environ.get("ARTIFACTS_SMOKE_EXCHANGE")
RUNNER_WAIT = 1500
PANEL = "[data-testid=artifacts-panel]"
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


def dashboard_panel(page):
    """The panel's frame inside the Hermes dashboard; the host can remount the tab while it settles."""
    last: Exception | None = None
    for _ in range(3):
        try:
            frame = page.wait_for_selector(PANEL, timeout=30_000).content_frame()
            frame.wait_for_selector(".ha-root", state="attached", timeout=15_000)
            return frame
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
            page.wait_for_timeout(1000)
    raise last


def redact(value):
    return json.loads(TOKEN_RE.sub("<share-token>", json.dumps(value)))


def get(url: str, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def git_identity() -> dict:
    run = lambda *a: subprocess.run(["git", "-C", str(PROJECT), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    return {"revision": run("rev-parse", "HEAD"), "dirty": run("status", "--porcelain") != ""}


def run_agent(work: Path, hermes_home: Path, max_turns: int) -> dict:
    """Resolve the subscription credential read-only and run one bounded agent. The record is credential-free."""
    model = yaml.safe_load((OPERATOR_HOME / "config.yaml").read_text())["model"]["default"]
    resolved = subprocess.run(
        [str(HERMES_PYTHON), "-c", RESOLVE], cwd=HERMES_CHECKOUT, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(OPERATOR_HOME)},
    )
    if resolved.returncode != 0:
        return {"model": model,
                "blocker": "read-only credential resolution failed: " + (resolved.stderr.strip().splitlines() or [""])[-1][:300]}
    credentials = json.loads(resolved.stdout)
    credentials["model"] = model
    runner = subprocess.run(
        [str(HERMES_PYTHON), str(PROJECT / "scripts" / "agent_runner.py"), str(work), str(max_turns)],
        cwd=work, input=json.dumps(credentials) + "\n", capture_output=True, text=True, timeout=1200,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(hermes_home)},
    )
    secret = credentials.pop("api_key")
    del credentials
    record = {"model": model, "returncode": runner.returncode, "credential_absent": secret not in runner.stdout + runner.stderr,
              "stdout": runner.stdout.replace(secret, "<redacted>"),
              "stderr": runner.stderr.replace(secret, "<redacted>")}
    del secret
    return record


def serve_runner(exchange: Path) -> int:
    """Credential host half of the split smoke: run the agent against the forwarded service."""
    request = json.loads((exchange / "request.json").read_text())
    root = SCRATCH / "runtime" / "agent-runner"
    if root.exists():
        shutil.rmtree(root)
    work = root / "agent-work"
    work.mkdir(parents=True)
    os.chmod(root, 0o700)
    host_before = host_status()
    hermes_home = root / "hermes-home"
    install_plugin(hermes_home, request["service_url"], request["token"])
    try:
        record = run_agent(work, hermes_home, min(int(request["max_turns"]), 15))
    except Exception as exc:  # noqa: BLE001 - reported to the browser host as a blocker
        record = {"blocker": f"runner host failed: {type(exc).__name__}: {str(exc)[-300:]}"}
    finally:
        record_files = {p.name: base64.b64encode(p.read_bytes()).decode() for p in work.iterdir() if p.is_file()}
        shutil.rmtree(root)
    record.update(files=record_files, runner=git_identity(), runner_plugin_tree_sha256=plugin_tree_hash(),
                  runner_checkout_unchanged=host_status() == host_before,
                  request_token_sha256=hashlib.sha256(request["token"].encode()).hexdigest())
    target = exchange / "outcome.json"
    target.with_suffix(".tmp").write_text(json.dumps(record))
    target.with_suffix(".tmp").rename(target)
    print("runner returncode", record.get("returncode"), "blocker" if "blocker" in record else "")
    return 0


def run_agent_remotely(exchange: Path, rt: Runtime, work: Path, max_turns: int) -> dict:
    request = exchange / "request.json"
    outcome = exchange / "outcome.json"
    outcome.unlink(missing_ok=True)
    request.write_text(json.dumps({"service_url": rt.service_url, "token": rt.token, "max_turns": max_turns}))
    os.chmod(request, 0o600)
    deadline = time.monotonic() + RUNNER_WAIT
    while not outcome.exists():
        if time.monotonic() > deadline:
            return {"blocker": f"no runner outcome within {RUNNER_WAIT}s"}
        time.sleep(2)
    record = json.loads(outcome.read_text())
    for name, content in record.pop("files", {}).items():
        if "/" not in name and name not in ("", ".", ".."):
            (work / name).write_bytes(base64.b64decode(content))
    return record


def main() -> int:
    refuse_live(SCRATCH)
    if "--reevaluate" in sys.argv:
        return reevaluate()
    if "--runner" in sys.argv:
        return serve_runner(Path(sys.argv[sys.argv.index("--runner") + 1]))
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
    checks = []
    identity = git_identity()
    report: dict = {"plugin_tree_sha256": plugin_tree_hash(), "max_turns": max_turns, **identity,
                    "runner_placement": "split" if EXCHANGE else "local"}
    try:
        rt.prepare()
        work = root / "agent-work"
        work.mkdir()
        rt.start_service()
        rt.start_dashboard()
        record = (run_agent_remotely(Path(EXCHANGE), rt, work, max_turns) if EXCHANGE
                  else run_agent(work, rt.hermes_home, max_turns))
        report["model"] = record.get("model")
        if "blocker" in record:
            report["blocker"] = record["blocker"]
            raise RuntimeError(report["blocker"])
        if EXCHANGE:
            checks.append(("runner host answered this run's request",
                           record.get("request_token_sha256") == hashlib.sha256(rt.token.encode()).hexdigest(), None))
            checks.append(("runner host ran the same clean revision and plugin",
                           record["runner"] == identity and not identity["dirty"]
                           and record["runner_plugin_tree_sha256"] == plugin_tree_hash(), record["runner"]))
            checks.append(("runner host Hermes checkout unchanged", record["runner_checkout_unchanged"], None))
        (root / "logs" / "agent-runner.err").write_text(record["stderr"])
        if record["returncode"] != 0 or not record["stdout"].strip():
            report["blocker"] = "agent runner failed: " + record["stderr"].strip()[-800:]
            raise RuntimeError(report["blocker"])
        outcome = json.loads(record["stdout"].strip().splitlines()[-1])
        checks.append(("credential never appears in runner output", record["credential_absent"], None))
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
                panel = dashboard_panel(page)
                panel.wait_for_selector("[data-testid=artifact-frame]", timeout=30_000)
                page.wait_for_timeout(1200)
                page.screenshot(path=str(EVIDENCE / "dashboard-agent-artifact.png"))
                page.goto(f"{rt.dashboard_url}/artifacts")
                panel = dashboard_panel(page)
                panel.wait_for_selector(".ha-row", timeout=30_000)
                for _ in range(20):
                    rows = len(panel.query_selector_all(".ha-row"))
                    if rows == len(listing):
                        break
                    page.wait_for_timeout(500)
                checks.append(("dashboard library lists both agent artifacts", rows == len(listing), rows))
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
