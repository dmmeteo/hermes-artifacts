"""Full project gate: `uv run --frozen python scripts/verify.py`.

Runs every required gate against isolated loopback runtimes and labeled Docker
resources only. Each gate's output is kept under
<scratch>/evidence/verify/<timestamp>-<revision>/. Exits 1 if any gate fails;
no gate is skipped silently.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_smoke import EVIDENCE as SMOKE_EVIDENCE, plugin_tree_hash  # noqa: E402
from runtime import HERMES_CHECKOUT, HERMES_PYTHON, PROJECT, SCRATCH, host_head, host_status, refuse_live  # noqa: E402

# Everything the recorded agent run exercised. The browser bundle is excluded: the UAT re-runs every
# dashboard check on the current bundle, so the recorded run only vouches for agent and backend behavior.
AGENT_CONTRACT = ["app", "plugin", "pyproject.toml", "uv.lock", "scripts/agent_runner.py", "scripts/runtime.py",
                  ":(exclude)plugin/dashboard/dist"]
HERMES_CONTRACT_TESTS = [
    "tests/hermes_cli/test_web_server.py::TestDashboardPluginManifestExtensions",
    "tests/hermes_cli/test_web_server.py::TestPluginAPIAuth",
    "tests/hermes_cli/test_plugin_runtime_disable_gate.py",
    "tests/hermes_cli/test_project_plugin_rce_bypass.py",
]


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True, text=True).stdout.strip()


def plugin_tree_hash_at(revision: str) -> str:
    """plugin_tree_hash() over the tracked plugin tree of a past revision."""
    digest = hashlib.sha256()
    for path in sorted(map(Path, git("ls-tree", "-r", "--name-only", revision, "plugin").splitlines())):
        content = subprocess.run(["git", "-C", str(PROJECT), "show", f"{revision}:{path}"], capture_output=True).stdout
        digest.update(str(path).encode() + b"\0" + content)
    return digest.hexdigest()


def agent_evidence(smoke: dict) -> dict:
    revision = smoke.get("revision") or "HEAD"
    return {
        "evidence": str(SMOKE_EVIDENCE / "report.json"), "smoke_revision": smoke.get("revision"),
        "recorded_hash_matches_smoke_revision": plugin_tree_hash_at(revision) == smoke.get("plugin_tree_sha256"),
        "full_plugin_tree_matches": smoke.get("plugin_tree_sha256") == plugin_tree_hash(),
        "reevaluated": smoke.get("reevaluated"), "smoke_tree_dirty": smoke.get("dirty"),
        "agent_contract_drift": git("diff", "--name-only", revision, "--", *AGENT_CONTRACT).splitlines()
        + git("ls-files", "--others", "--exclude-standard", "--", *AGENT_CONTRACT).splitlines(),
        "hint": "rerun scripts/agent_smoke.py only within the authorized bounded smoke",
    }


class Gates:
    def __init__(self, folder: Path):
        self.folder = folder
        self.results: list[dict] = []

    def command(self, name: str, argv: list[str], cwd: Path = PROJECT, timeout: int = 1800,
                env: dict | None = None, expect_in_output: str | None = None) -> bool:
        started = time.monotonic()
        log = self.folder / f"{len(self.results) + 1:02d}-{name}.log"
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = f"TIMEOUT after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}", 124
        log.write_text(f"$ {' '.join(argv)}\n(cwd {cwd})\n\n{output}")
        ok = code == 0 and (expect_in_output is None or expect_in_output in output)
        return self.record(name, ok, {"exit": code, "log": str(log), "seconds": round(time.monotonic() - started, 1)})

    def record(self, name: str, ok: bool, detail: object) -> bool:
        self.results.append({"gate": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(detail)[:300]}", flush=True)
        return ok


def main() -> int:
    refuse_live(SCRATCH)
    revision = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = SCRATCH / "evidence" / "verify" / f"{stamp}-{revision[:12]}"
    folder.mkdir(parents=True)
    gates = Gates(folder)
    host_before, head_before = host_status(), host_head()
    clean_env = {"PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent), "HOME": str(Path.home())}

    gates.record("Hermes checkout clean before gates", host_before == "",
                 {"head": head_before, "status": host_before[:300]})
    gates.command("uv lock is current", ["uv", "lock", "--check"])
    gates.command("service tests on Python 3.14", [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
                  expect_in_output="passed")
    gates.record("service interpreter is Python 3.14", sys.version_info[:2] == (3, 14), sys.version.split()[0])
    gates.command("plugin tests on host Python 3.11",
                  [str(HERMES_PYTHON), "-m", "pytest", "tests/plugin", "-q", "-p", "no:cacheprovider",
                   "-o", "addopts=", "--rootdir", "tests/plugin"], env=clean_env, expect_in_output="passed")
    gates.command("plugin imports under host Python 3.11",
                  [str(HERMES_PYTHON), "-c", "import sys, ast, pathlib; assert sys.version_info[:2] == (3, 11);"
                   " [ast.parse(p.read_text(), feature_version=(3, 11)) for p in pathlib.Path('plugin').rglob('*.py')];"
                   " print('ok', sys.version.split()[0])"], env=clean_env, expect_in_output="ok 3.11")
    gates.command("web dependencies install from lockfile", ["npm", "ci", "--no-audit", "--no-fund"],
                  cwd=PROJECT / "web")
    gates.command("web typecheck and bundle build", ["npm", "run", "build"], cwd=PROJECT / "web")
    gates.command("committed dashboard and console bundles match source",
                  ["git", "diff", "--exit-code", "--", "plugin/dashboard/dist", "app/console"])
    same = all((PROJECT / "app/console" / a).read_bytes() == (PROJECT / "plugin/dashboard/dist/console" / b).read_bytes()
               for a, b in (("console.html", "console.html"), ("console.js", "assets/console.js"),
                            ("console.css", "assets/console.css")))
    gates.record("standalone console and Hermes plugin ship byte-identical panel files", same, None)
    gates.command("Hermes dashboard plugin contract tests (isolated)",
                  [str(HERMES_PYTHON), "-m", "pytest", *HERMES_CONTRACT_TESTS, "-o", "addopts=", "-q",
                   "-p", "no:cacheprovider", "--basetemp", str(folder / "hermes-pytest")],
                  cwd=HERMES_CHECKOUT, env={**clean_env, "HERMES_HOME": str(folder / "hermes-test-home")},
                  expect_in_output="passed")
    gates.command("runtime UAT: real dashboard, guest service, browser, lifecycle",
                  [sys.executable, str(PROJECT / "scripts" / "uat.py")], expect_in_output="0 failed")
    gates.command("concept gate: standalone and embedded hosts, auth negative controls, packaged skill",
                  [sys.executable, str(PROJECT / "scripts" / "verify_concept.py")], expect_in_output=" 0 failed at ")
    gates.command("container build and smoke", [sys.executable, str(PROJECT / "scripts" / "container_smoke.py")])

    smoke = json.loads((SMOKE_EVIDENCE / "report.json").read_text()) if (SMOKE_EVIDENCE / "report.json").exists() else {}
    evidence = agent_evidence(smoke)
    gates.record("real agent smoke evidence passed for the current agent and backend source",
                 smoke.get("passed") is True and "reevaluated" not in smoke and smoke.get("dirty") is False
                 and evidence["recorded_hash_matches_smoke_revision"]
                 and not evidence["agent_contract_drift"], evidence)

    tracked = git("grep", "-nIE", r"(eyJhbGc[i]Oi|ARTIFACTS_API_TOKEN=[A-Za-z0-9_-]{16,}|HERMES_ARTIFACTS_TOKEN=[A-Za-z0-9_-]{16,}"
                  r"|\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53})")
    tracked += "\n".join(f for f in git("ls-files").splitlines()
                         if Path(f).name in ("api-token", "htpasswd", ".env") or f.startswith(".local"))
    gates.record("no credentials tracked in git", tracked.strip() == "", tracked[:300])

    gates.record("Hermes checkout unchanged by the gates",
                 host_status() == host_before and host_head() == head_before,
                 {"head_before": head_before, "head_after": host_head(), "status": host_status()[:300]})
    summary = {
        "revision": revision, "worktree_dirty": bool(dirty), "started": stamp, "evidence": str(folder),
        "hermes_head": head_before,
        "uat_report": str(SCRATCH / "evidence" / "uat" / "report.json"),
        "passed": sum(r["ok"] for r in gates.results), "failed": sum(not r["ok"] for r in gates.results),
        "gates": gates.results,
    }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    (SCRATCH / "evidence" / "verify" / "latest.json").write_text(json.dumps(summary, indent=2))
    print(f"verify: {summary['passed']} passed, {summary['failed']} failed at {revision[:12]}"
          f"{' (dirty worktree)' if dirty else ''}; evidence {folder}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
