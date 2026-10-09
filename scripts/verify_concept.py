"""Concept gate for AC1-AC8: `uv run --frozen python scripts/verify_concept.py`.

One independent service with the standalone console enabled serves both hosts. The gate first drives
it with no Hermes anywhere (Basic login from a synthetic bcrypt hash), then installs the plugin into two
fresh, isolated HERMES_HOMEs, discovers the packaged skill, starts one isolated dashboard and drives the
same panel inside its iframe. Negative controls back every fail-closed claim. Secrets are synthetic,
generated per run and never written to evidence. Evidence: <scratch>/evidence/concept/<stamp>-<rev>/.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import (HERMES_CHECKOUT, HERMES_PYTHON, PROJECT, SCRATCH, SERVICE_PYTHON, Runtime,  # noqa: E402
                     free_port, host_status, install_plugin, refuse_live, wait_http)
from uat import T, art, panel, rect, tolerant, viewport  # noqa: E402

# SHA-256 of the accepted data contract: schema, schema version, token/slug/profile/session formats and kinds.
# A deliberate contract change updates this value in the same commit.
ACCEPTED_CONTRACT = "9e0937d656aec806b24975272e81a03bfbd2ef5b3b8917d19b21711fba847bd6"
CONTRACT = (
    "import hashlib\nfrom app import domain, store\n"
    "parts = [store.SCHEMA, str(store.SCHEMA_VERSION), domain.SHARE_TOKEN_RE.pattern, domain.SLUG_RE.pattern,"
    " domain.PROFILE_RE.pattern, domain.SESSION_RE.pattern, ','.join(k.value for k in domain.Kind)]\n"
    "print(hashlib.sha256(chr(0).join(parts).encode()).hexdigest())\n"
)
FIXTURES = PROJECT / "tests" / "fixtures"
TOKEN_URL = re.compile(r"^(https?://[^/]+)/([A-Za-z0-9_-]{22})$")
CHANNEL = "hermes-artifacts"
checks: list[dict] = []
secrets_seen: dict[str, str] = {}


def check(name: str, ok: bool, detail: object = None) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {str(detail)[:400]}"), flush=True)
    return bool(ok)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class Gate:
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.screens = evidence / "screens"
        self.screens.mkdir(parents=True)

    def shot(self, page: Page, name: str) -> None:
        page.screenshot(path=str(self.screens / f"{name}.png"))


def policy_checks(folder: Path) -> None:
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/service/test_console.py", "-q", "-p",
                             "no:cacheprovider"], cwd=PROJECT, capture_output=True, text=True)
    (folder / "policy-tests.log").write_text(result.stdout + result.stderr)
    check("framework policy tests: config, Basic/bcrypt, Origin/CSRF, proxy headers, guest host", result.returncode == 0,
          result.stdout[-300:])
    contract = subprocess.run([str(SERVICE_PYTHON), "-c", CONTRACT], cwd=PROJECT, capture_output=True, text=True)
    check("no schema, schema-version, token/slug/profile/session format or kind change from the accepted contract",
          contract.returncode == 0 and contract.stdout.strip() == ACCEPTED_CONTRACT,
          contract.stdout.strip() or contract.stderr[-300:])
    imports = subprocess.run([str(SERVICE_PYTHON), "-c", "import hermes_cli"], capture_output=True, text=True)
    check("the service interpreter cannot import Hermes at all", imports.returncode != 0, imports.returncode)


def misconfig_checks(root: Path, token_file: Path, weak_hash: str, good_hash: str, password: str) -> None:
    """Every bad console configuration must stop the service before it listens."""
    port = free_port()
    base = {"PATH": "/usr/bin:/bin", "ARTIFACTS_DB": str(root / "misconfig.db"), "ARTIFACTS_PORT": str(port),
            "ARTIFACTS_API_TOKEN_FILE": str(token_file)}
    origin = f"http://127.0.0.1:{port}"
    cases = {
        "plaintext password variable": {"ARTIFACTS_CONSOLE_ORIGIN": origin, "ARTIFACTS_CONSOLE_PASSWORD": password},
        "plaintext in the hash variable": {"ARTIFACTS_CONSOLE_ORIGIN": origin, "ARTIFACTS_CONSOLE_USER": "operator",
                                           "ARTIFACTS_CONSOLE_PASSWORD_HASH": password},
        "weak bcrypt cost": {"ARTIFACTS_CONSOLE_ORIGIN": origin, "ARTIFACTS_CONSOLE_USER": "operator",
                             "ARTIFACTS_CONSOLE_PASSWORD_HASH": weak_hash},
        "origin without a login": {"ARTIFACTS_CONSOLE_ORIGIN": origin},
        "public origin over http": {"ARTIFACTS_CONSOLE_ORIGIN": "http://artifacts.example.com",
                                    "ARTIFACTS_CONSOLE_USER": "operator", "ARTIFACTS_CONSOLE_PASSWORD_HASH": good_hash},
        "login without an origin": {"ARTIFACTS_CONSOLE_USER": "operator", "ARTIFACTS_CONSOLE_PASSWORD_HASH": good_hash},
    }
    for label, extra in cases.items():
        try:
            result = subprocess.run([str(SERVICE_PYTHON), "-m", "app"], cwd=PROJECT, env={**base, **extra},
                                    capture_output=True, text=True, timeout=30)
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            output, code = "still running: it started listening", 0
        leaked = password in output or good_hash in output or weak_hash in output
        check(f"misconfiguration refuses to start: {label}", code != 0 and "ConfigError" in output and not leaked,
              {"exit": code, "leaked": leaked, "tail": output.strip().splitlines()[-1:] if not leaked else "redacted"})


def publish(service: str, token: str, name: str, title: str, profile: str, kind: str | None = None) -> dict:
    kinds = {".html": "html", ".md": "markdown", ".svg": "svg", ".png": "png", ".webp": "webp"}
    data = (FIXTURES / name).read_bytes() if (FIXTURES / name).exists() else name.encode()
    query = urllib.parse.urlencode({"title": title, "kind": kind or kinds[Path(name).suffix], "source_profile": profile})
    status, _, body = http("POST", f"{service}/internal/v1/artifacts?{query}", {"Authorization": f"Bearer {token}"}, data)
    assert status == 201, body
    return json.loads(body)


def standalone(gate: Gate, browser, service: str, token: str, password: str, published: dict) -> dict:
    origin = service
    host = origin.split("//", 1)[1]
    auth = "Basic " + base64.b64encode(f"operator:{password}".encode()).decode()
    secrets_seen.update({"password": password, "basic": auth.split()[1], "service_token": token})
    api = {"X-Artifacts-Console": "1"}

    for path in ("/console/", "/console/orbit-counter", "/console/assets/console.js", "/console/api/artifacts",
                 f"/console/api/artifacts/{published['orbit']['id']}/download"):
        code, headers, _ = http("GET", origin + path, api)
        challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
        check(f"standalone {path} without credentials is 401 with a Basic challenge",
              code == 401 and challenge.startswith("Basic"), [code, challenge])
    wrong = "Basic " + base64.b64encode(f"operator:{password}x".encode()).decode()
    check("standalone wrong password is 401", http("GET", origin + "/console/api/artifacts", {**api, "Authorization": wrong})[0] == 401)
    check("machine bearer token does not open the console API",
          http("GET", origin + "/console/api/artifacts", {**api, "Authorization": f"Bearer {token}"})[0] == 401)
    check("proxy identity headers do not authenticate", http("GET", origin + "/console/api/artifacts",
          {**api, "X-Forwarded-User": "operator", "Remote-User": "operator", "X-Forwarded-For": "127.0.0.1"})[0] == 401)
    for header in ("Basic", "Basic %%%", "Basic " + base64.b64encode(("operator:" + "ü" * 80).encode()).decode(),
                   "Basic " + base64.b64encode(b"\xff\xfe:\xfd").decode()):
        check(f"malformed Authorization {header[:16]!r}… is 401",
              http("GET", origin + "/console/api/artifacts", {**api, "Authorization": header})[0] == 401)
    good = {**api, "Authorization": auth}
    check("console API answers with the configured login", http("GET", origin + "/console/api/artifacts", good)[0] == 200)
    check("console API refuses calls without the panel header",
          http("GET", origin + "/console/api/artifacts", {"Authorization": auth})[0] == 403)
    shares = f"{origin}/console/api/artifacts/{published['orbit']['id']}/shares"
    body = json.dumps({"expires_in_seconds": 3600}).encode()
    for label, extra in (("no Origin", {}), ("foreign Origin", {"Origin": "https://evil.example"}),
                         ("null Origin", {"Origin": "null"}),
                         ("cross-site fetch", {"Origin": origin, "Sec-Fetch-Site": "cross-site"})):
        code = http("POST", shares, {**good, "Content-Type": "application/json", **extra}, body)[0]
        check(f"share creation with {label} is refused (CSRF)", code == 403, code)
    check("console refuses another Host (a separate guest host never serves it)",
          http("GET", origin + "/console/api/artifacts", {**good, "Host": "art.example.com"})[0] == 404)
    code, _, root = http("GET", origin + "/")
    check("public root lists nothing", code == 404 and b"Orbit" not in root and b"Quarterly" not in root, code)
    detail = json.loads(http("GET", f"{origin}/console/api/artifacts/{published['orbit']['id']}", good)[2])
    check("publishing created no share", detail["shares"] == [], detail["shares"])

    result: dict = {}
    anonymous = browser.new_context()
    response = anonymous.new_page().goto(origin + "/console/")
    check("browser without credentials gets 401 for the console", response.status == 401, response.status)
    anonymous.close()

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark",
                                  http_credentials={"username": "operator", "password": password})
    requests: list[str] = []
    context.on("request", lambda r: requests.append(r.url))
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(origin + "/console/")
    page.wait_for_selector(".ha-row", timeout=T(20_000))
    rows = page.locator(".ha-row").count()
    result["rows"] = rows
    check("standalone library lists the shared library", rows == len(published), rows)
    titles = page.locator(".ha-row__title").all_inner_texts()
    metas = " ".join(page.locator(".ha-row__meta").all_inner_texts())
    check("attribution from several profiles shows in one library",
          all(name in metas for name in ("prime", "steve", "pewds")), titles[:3])
    overflow = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
    check("wide library with long metadata has no horizontal overflow", overflow <= 1, overflow)
    gate.shot(page, "standalone-desktop-dark-library")
    page.focus("input[type=search]")
    focused = {}
    for _ in range(12):
        page.keyboard.press("Tab")
        focused = page.evaluate("() => ({cls: document.activeElement.className,"
                                " outline: getComputedStyle(document.activeElement).outlineStyle})")
        if "ha-row" in str(focused["cls"]):
            break
    check("standalone keyboard reaches rows with a visible focus ring",
          "ha-row" in str(focused["cls"]) and focused["outline"] != "none", focused)
    gate.shot(page, "standalone-desktop-dark-keyboard-focus")

    page.goto(f"{origin}/console/{published['orbit']['slug']}")
    page.wait_for_selector("[data-testid=artifact-frame]", timeout=T(20_000))
    frame = art(page, page.main_frame)
    for _ in range(3):
        frame.locator("#increment").click()
    check("standalone interactive HTML runs", frame.locator("#count").inner_text() == "3")
    page.wait_for_timeout(T(800))
    probes = json.loads(frame.locator("#probes").inner_text())
    check("standalone artifact runs in an opaque origin", probes.get("origin") == "null", probes.get("origin"))
    for key in ("cookie", "localStorage", "parentDocument", "topLocation", "dashboardApi", "managementApi", "externalFetch"):
        check(f"standalone artifact cannot reach {key}", str(probes.get(key, "")).startswith("blocked"), probes.get(key))
    sandbox = page.get_attribute("[data-testid=artifact-frame]", "sandbox")
    check("standalone preview frame is sandboxed without same-origin", sandbox == "allow-scripts", sandbox)
    shot_box = rect(page, "[data-testid=artifact-frame]", page.main_frame)
    column = rect(page, ".ha-root", page.main_frame)
    check("standalone preview fills the console column", shot_box["width"] >= 0.98 * column["width"]
          and shot_box["y"] + shot_box["height"] >= viewport(page)["height"] - 20, [shot_box, column])
    gate.shot(page, "standalone-desktop-dark-viewer")

    page.click("text=Full window")
    page.wait_for_selector("[data-mode=expanded]")
    box, view = rect(page, "[data-testid=artifact-frame]", page.main_frame), viewport(page)
    check("standalone full window spans the viewport and keeps state",
          abs(box["width"] - view["width"]) <= 2 and frame.locator("#count").inner_text() == "3", box)
    gate.shot(page, "standalone-desktop-dark-full-window")
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-mode=inline]")
    page.click("button[aria-label=Fullscreen]")
    page.wait_for_function("() => document.fullscreenElement?.dataset.testid === 'viewer'", timeout=T(10_000))
    check("standalone Fullscreen engages on the trusted viewer and keeps state", frame.locator("#count").inner_text() == "3")
    gate.shot(page, "standalone-desktop-dark-fullscreen")
    page.click("button[aria-label='Exit fullscreen']")
    page.wait_for_function("() => document.fullscreenElement === null", timeout=T(10_000))

    frame.locator("#self-nav").click()
    page.wait_for_selector("text=tried to navigate away", timeout=T(15_000))
    page.wait_for_timeout(T(800))
    check("standalone frame self-navigation is blocked and the artifact restored",
          art(page, page.main_frame).locator("#count").inner_text() == "0")

    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    page.click("button:has-text('Copy link')")
    page.wait_for_timeout(T(300))
    copied = page.evaluate("() => navigator.clipboard.readText()")
    check("standalone private link is the console slug URL", copied == f"{origin}/console/{published['orbit']['slug']}", copied)

    with page.expect_download() as download:
        page.click("button[aria-label=Download]")
    saved = gate.evidence / "standalone-download.html"
    download.value.save_as(saved)
    check("standalone download returns the original bytes",
          saved.read_bytes() == (FIXTURES / "interactive.html").read_bytes(), download.value.suggested_filename)
    saved.unlink()

    page.click("[data-testid=viewer] .ha-toolbar__actions button:has-text('Share')")
    dialog = page.locator("dialog.ha-share[open]")
    dialog.wait_for()
    check("standalone share dialog takes focus",
          page.evaluate("() => document.querySelector('dialog.ha-share[open]').contains(document.activeElement)"))
    dialog.locator("button:has-text('Create link')").click()
    dialog.locator("[data-testid=share-grant]").wait_for(timeout=T(10_000))
    url = dialog.locator("input[aria-label='Share link']").input_value()
    match = TOKEN_URL.match(url)
    check("standalone Share returns a 22-character guest link on the configured public host",
          bool(match) and match.group(1) == origin, len(url))
    gate.shot(page, "standalone-desktop-dark-share-created")
    guest = browser.new_context(viewport={"width": 390, "height": 844})
    gpage = guest.new_page()
    check("guest opens the link without the console login", gpage.goto(url).status == 200)
    gpage.wait_for_selector("#menu-toggle", timeout=T(10_000))
    check("guest viewer keeps the bottom-centre ellipsis menu",
          gpage.evaluate("() => { const r = document.querySelector('#menu-toggle').getBoundingClientRect();"
                         " return Math.abs(r.left + r.width / 2 - innerWidth / 2) < 2 && r.bottom > innerHeight - 80; }"))
    gate.shot(gpage, "standalone-guest-mobile")
    dialog.locator(".ha-share__revoke").first.click()
    confirm = page.locator("dialog.ha-confirm[open]")
    confirm.wait_for()
    confirm.locator("button:has-text('Revoke')").click()
    dialog.locator(".ha-share__status:has-text('Share link revoked.')").wait_for(timeout=T(10_000))
    check("revoked standalone link is unavailable to the guest", gpage.goto(url).status == 404)
    guest.close()
    page.keyboard.press("Escape")
    result["share_tokens"] = [match.group(2)] if match else []

    storage = page.evaluate("() => JSON.stringify([document.cookie, Object.entries(localStorage), Object.entries(sessionStorage)])")
    leaked = [k for k, v in secrets_seen.items() if v in storage or any(v in r for r in requests)]
    check("no credential in standalone URLs, cookies or storage", not leaked, leaked)
    check("no uncaught errors in the standalone console", not errors, errors[:3])
    context.close()

    narrow = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light", is_mobile=True,
                                 has_touch=True, device_scale_factor=2, reduced_motion="reduce",
                                 http_credentials={"username": "operator", "password": password})
    npage = narrow.new_page()
    npage.goto(origin + "/console/")
    npage.wait_for_selector(".ha-row")
    overflow = npage.evaluate("() => document.documentElement.scrollWidth - innerWidth")
    check("390px standalone library has no horizontal overflow", overflow <= 1, overflow)
    scheme = npage.evaluate("() => getComputedStyle(document.documentElement).colorScheme")
    check("standalone follows the light colour scheme", scheme == "light", scheme)
    motion = npage.evaluate("() => getComputedStyle(document.querySelector('.ha-btn')).transitionDuration")
    check("reduced motion removes transitions", motion in ("0s", ""), motion)
    gate.shot(npage, "standalone-mobile-light-library")
    npage.goto(f"{origin}/console/{published['long']['slug']}")
    npage.wait_for_selector("[data-testid=artifact-frame]")
    npage.wait_for_timeout(T(500))
    toolbar = npage.evaluate("() => { const t = document.querySelector('.ha-toolbar'); return t.scrollWidth - t.clientWidth; }")
    check("390px viewer toolbar holds a long title without overflow", toolbar <= 1, toolbar)
    check("390px viewer has no page overflow", npage.evaluate("() => document.documentElement.scrollWidth - innerWidth") <= 1)
    gate.shot(npage, "standalone-mobile-light-long-title")
    narrow.close()
    return result


def hermes_skill_checks(rt: Runtime, folder: Path, service: str, token: str) -> None:
    second = rt.root / "hermes-home-beta"
    install_plugin(second, service, token)
    reports = {}
    for profile, home in (("alpha", rt.hermes_home), ("beta", second)):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(home), "HERMES_PROFILE_NAME": profile}
        result = subprocess.run([str(HERMES_PYTHON), str(PROJECT / "scripts" / "host_skill.py"), f"Note from {profile}"],
                                cwd=HERMES_CHECKOUT, env=env, capture_output=True, text=True, timeout=300)
        (folder / f"host-skill-{profile}.err").write_text(result.stderr[-20_000:])
        if not check(f"host skill check ran in fresh profile {profile}", result.returncode == 0, result.stderr[-500:]):
            continue
        report = json.loads(result.stdout.strip().splitlines()[-1])
        reports[profile] = report
        check(f"{profile}: plugin registers the packaged skill", report["plugin_skills"] == ["operating"], report["plugin_skills"])
        check(f"{profile}: skills_list shows artifacts:operating", report["skills_list_has_operating"], report["skills_list_entry"])
        check(f"{profile}: skill_view('artifacts:operating') returns the guide",
              report["skill_view_success"] is True and len(report["skill_view_mentions"]) == 6, report["skill_view_mentions"])
        check(f"{profile}: the skill is read from the plugin, not copied into the home", report["skill_not_copied_into_home"])
        check(f"{profile}: tool descriptions point to the skill", report["tool_pointer"])
        check(f"{profile}: attribution records the publishing profile", report["published_profile"] == profile,
              report["published_profile"])
    (folder / "host-skill.json").write_text(json.dumps(reports, indent=2))
    if len(reports) == 2:
        check("two profiles see one shared library",
              {"Note from alpha", "Note from beta"} <= set(reports["beta"]["library_titles"])
              and "Orbit counter" in reports["alpha"]["library_titles"], reports["beta"]["library_titles"][:8])


RECORD_MESSAGES = """(() => { window.__haMessages = [];
  addEventListener('message', e => { try { window.__haMessages.push(JSON.stringify(e.data, (k, v) =>
    v instanceof ArrayBuffer ? new TextDecoder().decode(v) : v)); } catch (err) {} }, true); })();"""


def embedded(gate: Gate, browser, rt: Runtime, origin: str, password: str, published: dict) -> None:
    dash = rt.dashboard_url
    for path in ("/api/plugins/artifacts/artifacts", f"/api/plugins/artifacts/artifacts/{published['orbit']['id']}/download"):
        check(f"anonymous {path} is refused by Hermes auth", http("GET", dash + path)[0] == 401)
    hermes_js = http("GET", f"{dash}/dashboard-plugins/artifacts/dist/console/assets/console.js")
    auth = {"Authorization": "Basic " + base64.b64encode(f"operator:{password}".encode()).decode()}
    standalone_js = http("GET", f"{origin}/console/assets/console.js", auth)
    check("Hermes and the standalone console serve byte-identical panel bundles",
          hermes_js[0] == standalone_js[0] == 200 and hermes_js[2] == standalone_js[2],
          hashlib.sha256(hermes_js[2]).hexdigest()[:16])
    bundle = hermes_js[2].decode("utf-8", "replace")
    check("the panel bundle references no Hermes window globals",
          "__HERMES" not in bundle and "HERMES_SESSION" not in bundle, None)
    check("no credential in the panel or adapter bundles",
          not any(v in bundle or v in (PROJECT / "plugin/dashboard/dist/index.js").read_text() for v in secrets_seen.values()))

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(RECORD_MESSAGES)
    requests: list[str] = []
    context.on("request", lambda r: requests.append(r.url))
    page = context.new_page()
    page.goto(f"{dash}/artifacts")
    page.wait_for_load_state("networkidle")
    ui = panel(page)
    ui.wait_for_selector(".ha-row", timeout=T(30_000))
    session = page.evaluate("() => window.__HERMES_SESSION_TOKEN__ || ''")
    check("the dashboard runs with its own session token", len(session) >= 16, len(session))
    rows = ui.locator(".ha-row").count()
    check("embedded panel lists the same shared library", rows >= len(published), rows)
    gate.shot(page, "embedded-desktop-dark-library")
    page.goto(f"{dash}/artifacts/{published['orbit']['slug']}")
    ui = panel(page)
    ui.wait_for_selector("[data-testid=artifact-frame]", timeout=T(30_000))
    frame = art(page, ui)
    frame.locator("#increment").click()
    check("embedded interactive HTML runs", frame.locator("#count").inner_text() == "1")
    check("embedded preview minted no guest grant",
          json.loads(http("GET", f"{origin}/internal/v1/artifacts/{published['orbit']['id']}",
                          {"Authorization": f"Bearer {secrets_seen['service_token']}"})[2])["artifact"]["active_shares"] == 0)
    gate.shot(page, "embedded-desktop-dark-viewer")
    ui.click("[data-testid=viewer] .ha-toolbar__actions button:has-text('Share')")
    dialog = ui.locator("dialog.ha-share[open]")
    dialog.wait_for()
    dialog.locator("button:has-text('Create link')").click()
    dialog.locator("[data-testid=share-grant]").wait_for(timeout=T(10_000))
    url = dialog.locator("input[aria-label='Share link']").input_value()
    check("embedded Share returns a 22-character guest link", bool(TOKEN_URL.match(url)), len(url))
    gate.shot(page, "embedded-desktop-dark-share-created")
    dialog.locator(".ha-share__revoke").first.click()
    ui.locator("dialog.ha-confirm[open] button:has-text('Revoke')").click()
    dialog.locator(".ha-share__status:has-text('Share link revoked.')").wait_for(timeout=T(10_000))
    check("embedded revoke makes the link unavailable", http("GET", url)[0] == 404)
    page.keyboard.press("Escape")

    before = page.url
    for slug in ("//evil.example/x", "../../settings", "orbit-counter?x=1", "javascript:alert(1)"):
        ui.evaluate("s => parent.postMessage({channel: 'hermes-artifacts', type: 'navigate', slug: s}, location.origin)", slug)
    page.wait_for_timeout(T(500))
    check("host ignores hostile navigation targets from the panel", page.url == before, page.url)
    status = ui.evaluate("""() => new Promise(done => { addEventListener('message', e => {
        if (e.data && e.data.id === 990001) done(e.data.status); });
        parent.postMessage({channel: 'hermes-artifacts', type: 'request', id: 990001, method: 'GET',
                            path: '/../../env'}, location.origin); })""")
    check("host refuses panel requests outside the Artifacts API", status == 400, status)
    status = ui.evaluate("""() => new Promise(done => { addEventListener('message', e => {
        if (e.data && e.data.id === 990002) done(e.data.status); });
        parent.postMessage({channel: 'hermes-artifacts', type: 'request', id: 990002, method: 'DELETE',
                            path: '/artifacts'}, location.origin); })""")
    check("host refuses methods the panel does not use", status == 400, status)

    messages = ui.evaluate("() => window.__haMessages.join('\\n')") + page.evaluate("() => window.__haMessages.join('\\n')")
    (gate.evidence / "embedded-message-kinds.json").write_text(json.dumps(sorted(set(
        re.findall(r'"type":"([a-z]+)"', messages)))))
    check("postMessage traffic between host and panel was recorded", '"type":"init"' in messages and '"type":"request"' in messages)
    leaked = [name for name, value in {**secrets_seen, "hermes_session": session}.items() if value and value in messages]
    check("no session token or credential crosses postMessage", not leaked, leaked)
    storage = ui.evaluate("() => JSON.stringify([document.cookie, Object.entries(localStorage), Object.entries(sessionStorage)])")
    check("the panel frame stores no session token or credential",
          not any(v and v in storage for v in [session, *secrets_seen.values()]), None)
    check("no credential appears in any request URL",
          not any(v and v in r for r in requests for v in [session, *secrets_seen.values()]), None)
    globals_seen = ui.evaluate("() => typeof window.__HERMES_PLUGIN_SDK__ + typeof window.__HERMES_SESSION_TOKEN__")
    check("the hosted panel has no Hermes globals in its window", globals_seen == "undefinedundefined", globals_seen)
    context.close()

    mobile = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True,
                                 has_touch=True, device_scale_factor=2)
    mpage = mobile.new_page()
    mpage.goto(f"{dash}/artifacts/{published['long']['slug']}")
    mpage.wait_for_load_state("networkidle")
    mui = panel(mpage)
    mui.wait_for_selector("[data-testid=artifact-frame]", timeout=T(30_000))
    mpage.wait_for_timeout(T(500))
    check("390px embedded viewer has no overflow with a long title",
          mui.evaluate("() => document.documentElement.scrollWidth - innerWidth") <= 1
          and mpage.evaluate("() => document.documentElement.scrollWidth - innerWidth") <= 1)
    gate.shot(mpage, "embedded-mobile-dark-long-title")
    mobile.close()


def guest_preservation(service_restart, origin: str, token: str, published: dict) -> None:
    bearer = {"Authorization": f"Bearer {token}"}
    status, _, body = http("POST", f"{origin}/internal/v1/artifacts/{published['report']['id']}/shares", {
        **bearer, "Content-Type": "application/json"}, json.dumps({"expires_in_seconds": 86_400}).encode())
    grant = json.loads(body)
    link = TOKEN_URL.match(grant["url"])
    check("tool-created share keeps the 22-character base64url token", status == 201 and bool(link), grant.get("url", "")[-30:])
    short = json.loads(http("POST", f"{origin}/internal/v1/artifacts/{published['svg']['id']}/shares", {
        **bearer, "Content-Type": "application/json"}, json.dumps({"expires_in_seconds": 1}).encode())[2])
    before = json.loads(http("GET", f"{origin}/internal/v1/artifacts/{published['report']['id']}", bearer)[2])["artifact"]
    service_restart()
    after = json.loads(http("GET", f"{origin}/internal/v1/artifacts/{published['report']['id']}", bearer)[2])["artifact"]
    check("artifact id, slug and bytes survive a restart",
          {k: before[k] for k in ("id", "slug", "content_sha256")} == {k: after[k] for k in ("id", "slug", "content_sha256")})
    check("an existing guest link still opens after a restart", http("GET", grant["url"])[0] == 200)
    time.sleep(2.2)
    for suffix in ("", "/content", "/download"):
        for method in ("GET", "HEAD"):
            check(f"expired link {method} {suffix or '/'} fails closed", http(method, short["url"] + suffix)[0] == 404)
    http("DELETE", f"{origin}/internal/v1/shares/{grant['share']['id']}", bearer)
    check("revoked link fails closed", http("GET", grant["url"])[0] == 404)
    check("malformed link fails closed", http("GET", f"{origin}/not-a-token")[0] == 404)


def recipe(folder: Path) -> None:
    helper = PROJECT / ".claude" / "skills" / "verify-hermes-artifacts" / "scripts" / "recipe.py"
    scratch = SCRATCH / "runtime" / "recipe"
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}, "ARTIFACTS_SCRATCH": str(SCRATCH)}
    for step in ("launch", "doctor", "drive", "cleanup"):
        result = subprocess.run([sys.executable, str(helper), step, "--root", str(scratch)], cwd=PROJECT, env=env,
                                capture_output=True, text=True, timeout=600)
        (folder / f"recipe-{step}.log").write_text(result.stdout + result.stderr)
        check(f"verification recipe step {step} succeeds", result.returncode == 0, (result.stdout + result.stderr)[-400:])
    evidence = sorted((SCRATCH / "evidence" / "recipe").glob("*/report.json"))
    report = json.loads(evidence[-1].read_text()) if evidence else {}
    check("recipe evidence survives its cleanup",
          bool(evidence) and report.get("passed") is True and not scratch.exists(), str(evidence[-1:]))
    refused = subprocess.run([sys.executable, str(helper), "doctor", "--root", str(Path.home() / ".hermes")],
                             cwd=PROJECT, env=env, capture_output=True, text=True)
    check("recipe refuses a live Hermes home as its root", refused.returncode != 0 and "refusing" in refused.stderr,
          refused.stderr[-200:])


def main() -> int:
    refuse_live(SCRATCH)
    revision = git("rev-parse", "HEAD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = SCRATCH / "evidence" / "concept" / f"{stamp}-{revision[:12]}"
    gate = Gate(folder)
    root = SCRATCH / "runtime" / "concept"
    if root.exists():
        shutil.rmtree(root)
    host_before = host_status()
    rt = Runtime(root)
    rt.root.mkdir(parents=True)
    os.chmod(rt.root, 0o700)
    (rt.root / "logs").mkdir()
    token_file = rt.root / "service" / "api-token"
    token_file.parent.mkdir()
    token_file.write_text(rt.token)
    os.chmod(token_file, 0o600)
    password = "synthetic-" + secrets.token_urlsafe(18)
    line = subprocess.run([str(SERVICE_PYTHON), "-m", "app.hash_password", "operator"], cwd=PROJECT,
                          input=password + "\n", capture_output=True, text=True, check=True).stdout.strip()
    htpasswd = rt.root / "service" / "htpasswd"
    htpasswd.write_text(line + "\n")
    os.chmod(htpasswd, 0o600)
    good_hash = line.split(":", 1)[1]
    weak = subprocess.run([str(SERVICE_PYTHON), "-c", "import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.read().encode(),"
                           " bcrypt.gensalt(6)).decode())"], input=password, capture_output=True, text=True, check=True).stdout.strip()
    origin = rt.service_url
    console_env = {"ARTIFACTS_CONSOLE_ORIGIN": origin, "ARTIFACTS_CONSOLE_HTPASSWD_FILE": str(htpasswd)}
    try:
        policy_checks(folder)
        misconfig_checks(rt.root, token_file, weak, good_hash, password)
        rt.start_service(extra_env=console_env)
        check("standalone service started with no HERMES_HOME and no Hermes install in its runtime",
              not rt.hermes_home.exists() and "HERMES_HOME" not in os.environ, None)
        published = {
            "orbit": publish(origin, rt.token, "interactive.html", "Orbit counter", "prime"),
            "report": publish(origin, rt.token, "report.md", "Quarterly delivery review", "steve"),
            "svg": publish(origin, rt.token, "diagram.svg", "Architecture sketch", "prime"),
            "long": publish(origin, rt.token, "# Long\n", ("A very long artifact title that keeps going to test "
                            "wrapping in the library rows and the viewer toolbar on phones, ") + "and long metadata beside it, end", "pewds",
                            kind="markdown"),
        }
        with sync_playwright() as playwright:
            browser = tolerant(playwright.chromium.launch())
            try:
                standalone(gate, browser, origin, rt.token, password, published)
                rt.prepare()
                hermes_skill_checks(rt, folder, origin, rt.token)
                rt.start_dashboard()
                embedded(gate, browser, rt, origin, password, published)
            finally:
                browser.close()

        def restart():
            rt.stop("service")
            rt.start_service(extra_env=console_env)
        guest_preservation(restart, origin, rt.token, published)
        recipe(folder)
    except Exception:  # noqa: BLE001 - an unexpected exception is a failed gate, with its trace kept
        check("concept gate completed without an unexpected exception", False, traceback.format_exc()[-3000:])
    finally:
        rt.stop_all()
        logs = "".join(p.read_text(errors="replace") for p in (rt.root / "logs").glob("*.log"))
        leaked = [k for k, v in secrets_seen.items() if v in logs]
        check("no credential in service or dashboard logs", not leaked, leaked)
        evidence_text = "".join(p.read_text(errors="replace") for p in folder.rglob("*") if p.is_file()
                                and p.suffix in (".json", ".log", ".err", ".txt"))
        check("no credential in the evidence folder", not any(v in evidence_text for v in secrets_seen.values()))
        check("Hermes checkout unchanged", host_status() == host_before, host_status()[:300])
        shutil.rmtree(rt.root, ignore_errors=True)
        check("concept runtime removed; evidence kept", not rt.root.exists() and folder.exists(), str(folder))
    report = {"revision": revision, "dirty": bool(git("status", "--porcelain")), "evidence": str(folder),
              "passed": sum(c["ok"] for c in checks), "failed": sum(not c["ok"] for c in checks), "checks": checks}
    (folder / "report.json").write_text(json.dumps(report, indent=2))
    (SCRATCH / "evidence" / "concept" / "latest.json").write_text(json.dumps(report, indent=2))
    print(f"concept: {report['passed']} passed, {report['failed']} failed at {revision[:12]}; evidence {folder}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
