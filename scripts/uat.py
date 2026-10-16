"""Runtime UAT against a real isolated Hermes dashboard and the artifact service.

Starts both processes on loopback, publishes fixtures through real Hermes tool
dispatch, then drives Chromium through the dashboard and the guest viewer.
Every check is recorded in evidence/uat/report.json; any failure exits 1.
Usage: python scripts/uat.py [--keep] [--executable PATH]   (PATH: e.g. /usr/bin/google-chrome)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import (HERMES_CHECKOUT, HERMES_PYTHON, PROJECT, SCRATCH, SERVICE_PYTHON, Runtime,  # noqa: E402
                     free_port, host_status, refuse_live)

EVIDENCE = SCRATCH / "evidence" / "uat"
SCREENS = EVIDENCE / "screens"
FIXTURES = PROJECT / "tests" / "fixtures"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

checks: list[dict] = []
SLUGS: dict[str, str] = {}
EXECUTABLE = sys.argv[sys.argv.index("--executable") + 1] if "--executable" in sys.argv else None
measurements: dict[str, object] = {}
# Browser waits are latency budgets, not assertions. On a host that other jobs load past its CPU count
# they scale with the 1-minute load per CPU (1x..6x); every check still has to hold.
LOAD_SCALE = min(6.0, max(1.0, os.getloadavg()[0] / (os.cpu_count() or 1)))


def T(ms: int) -> int:
    return int(ms * LOAD_SCALE)


def tolerant(browser):
    """Apply the scaled Playwright defaults (actions and navigation) to every page the browser opens."""
    def scaled(target):
        target.set_default_timeout(T(30_000))
        target.set_default_navigation_timeout(T(30_000))
        return target
    new_context, new_page = browser.new_context, browser.new_page
    browser.new_context = lambda *a, **k: scaled(new_context(*a, **k))
    browser.new_page = lambda *a, **k: scaled(new_page(*a, **k))
    return browser


def check(name: str, ok: bool, detail: object = None) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"), flush=True)
    return bool(ok)


def http(method: str, url: str, token: str | None = None, body: bytes | None = None,
         headers: dict | None = None) -> tuple[int, dict, bytes]:
    all_headers = dict(headers or {})
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=T(20)) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def shot(page: Page, name: str) -> str:
    path = SCREENS / f"{name}.png"
    page.screenshot(path=str(path))
    return str(path)


PANEL = "[data-testid=artifacts-panel]"


def panel(page: Page):
    """The shared panel's document. In Hermes it is the host's iframe; standalone it is the page itself."""
    if page.query_selector(PANEL) is None and page.query_selector(".ha-root"):
        return page.main_frame
    last: Exception | None = None
    for _ in range(5):
        try:
            frame = page.wait_for_selector(PANEL, timeout=T(30_000)).content_frame()
            frame.wait_for_selector(".ha-root", state="attached", timeout=T(15_000))
            return frame
        except Exception as exc:  # noqa: BLE001  the host remounted the tab while it settled
            last = exc
            page.wait_for_timeout(T(300))
    raise RuntimeError(f"panel iframe did not settle: {last}")


def offset(page: Page, ui) -> list[float]:
    """Page coordinates of the panel document's origin."""
    if ui == page.main_frame:
        return [0.0, 0.0]
    return page.eval_on_selector(PANEL, "e => { const r = e.getBoundingClientRect();"
                                 " return [r.left + e.clientLeft, r.top + e.clientTop]; }")


def rect(page: Page, selector: str, ui=None) -> dict:
    """A panel element's box in page coordinates (the page itself where no panel iframe exists, e.g. the guest viewer)."""
    ui = ui or (panel(page) if page.query_selector(PANEL) else page.main_frame)
    box = ui.eval_on_selector(selector, "e => { const r = e.getBoundingClientRect();"
                              " return {x: r.x, y: r.y, width: r.width, height: r.height}; }")
    dx, dy = offset(page, ui)
    return {**box, "x": box["x"] + dx, "y": box["y"] + dy}


def viewport(page: Page) -> dict:
    return page.evaluate("() => ({width: innerWidth, height: innerHeight})")


class ArtifactFrame:
    """The untrusted artifact: inner frame of the dashboard's trusted wrapper frame.

    Full Chrome runs the sandboxed wrapper out of process, and Playwright then loses the wrapper's
    srcdoc child, so its frame locators never resolve. Chrome's own frame tree is correct, so the
    artifact is read through CDP in an isolated world and clicked with real mouse input once both the
    artifact and the dashboard hit-test the target at that point.
    """

    OUTER = "[data-testid=artifact-frame]"

    def __init__(self, page: Page, ui=None):
        self.page = page
        self.fixed = ui

    @property
    def ui(self):
        """Re-resolved on every use: a page load replaces the panel iframe."""
        return self.fixed or panel(self.page)

    def locator(self, selector: str) -> "ArtifactLocator":
        return ArtifactLocator(self, selector)

    def _session(self):
        wrapper = self.ui.locator(self.OUTER).element_handle(timeout=T(10_000)).content_frame()
        page_session = self.page.context.new_cdp_session(self.page)
        try:
            context = {}
            if self.ui != self.page.main_frame:
                frames = [page_session.send("Page.getFrameTree")["frameTree"]]
                panel_id = None
                while frames:
                    node = frames.pop()
                    if node["frame"].get("url", "").endswith("/console/console.html"):
                        panel_id = node["frame"]["id"]
                    frames += node.get("childFrames", [])
                context = {"contextId": page_session.send("Page.createIsolatedWorld", {
                    "frameId": panel_id, "worldName": "uat-panel"})["executionContextId"]}
            owner = page_session.send("Runtime.evaluate", {
                "expression": f"document.querySelector('{self.OUTER}')", **context})
            wrapper_id = page_session.send("DOM.describeNode", {"objectId": owner["result"]["objectId"]})["node"]["frameId"]
        finally:
            page_session.detach()
        try:
            return self.page.context.new_cdp_session(wrapper), wrapper_id
        except Exception:  # noqa: BLE001  in-process wrapper: no separate CDP target
            return self.page.context.new_cdp_session(self.page), wrapper_id

    def evaluate(self, expression: str, timeout: float = 10_000):
        """Evaluate `expression` (sees `wrapper` and `doc`) once the artifact frame exists and it returns non-null."""
        deadline, last = time.monotonic() + timeout / 1000, None
        while time.monotonic() < deadline:
            try:
                session, wrapper_id = self._session()
                try:
                    nodes = [session.send("Page.getFrameTree")["frameTree"]]
                    while nodes and nodes[0]["frame"]["id"] != wrapper_id:
                        nodes = nodes[1:] + nodes[0].get("childFrames", [])
                    children = nodes[0].get("childFrames", []) if nodes else []
                    if len(children) == 1:
                        contexts = [session.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "uat"})
                                    ["executionContextId"] for frame_id in (wrapper_id, children[0]["frame"]["id"])]
                        wrapper = session.send("Runtime.evaluate", {
                            "expression": "(() => { const r = document.querySelector('iframe').getBoundingClientRect();"
                                          " return [r.left, r.top]; })()",
                            "contextId": contexts[0], "returnByValue": True})["result"]["value"]
                        result = session.send("Runtime.evaluate", {
                            "expression": f"((wrapper, doc) => ({expression}))({json.dumps(wrapper)}, document)",
                            "contextId": contexts[1], "returnByValue": True, "awaitPromise": True})
                        if "exceptionDetails" in result:
                            last = result["exceptionDetails"].get("exception", {}).get("description")
                        elif result["result"].get("value") is not None:
                            return result["result"]["value"]
                finally:
                    session.detach()
            except Exception as exc:  # noqa: BLE001  the frame is (re)loading
                last = str(exc)
            time.sleep(0.2)
        raise TimeoutError(f"artifact frame: {expression[:120]} did not resolve: {last}")


class ArtifactLocator:
    def __init__(self, frame: ArtifactFrame, selector: str):
        self.frame, self.selector = frame, json.dumps(selector)

    def inner_text(self, timeout: float = 10_000) -> str:
        return self.frame.evaluate(f"doc.querySelector({self.selector})?.innerText ?? null", timeout)

    def count(self) -> int:
        return self.frame.evaluate(f"doc.querySelectorAll({self.selector}).length")

    def evaluate(self, function: str):
        return self.frame.evaluate(f"(() => {{ const e = doc.querySelector({self.selector});"
                                   f" return e ? {{v: ({function})(e)}} : null; }})()")["v"]

    def click(self, timeout: float = 30_000) -> None:
        point = self.frame.evaluate(
            f"(() => {{ const e = doc.querySelector({self.selector}); if (!e) return null;"
            " e.scrollIntoView({block: 'center', inline: 'center'}); const r = e.getBoundingClientRect();"
            " const x = r.left + r.width / 2, y = r.top + r.height / 2, hit = doc.elementFromPoint(x, y);"
            " return r.width && r.height && hit && (hit === e || e.contains(hit)) ? [wrapper[0] + x, wrapper[1] + y] : null; })()",
            timeout)
        ui, page = self.frame.ui, self.frame.page
        outer = ui.eval_on_selector(
            ArtifactFrame.OUTER, "e => { const r = e.getBoundingClientRect(); return [r.left + e.clientLeft, r.top + e.clientTop]; }")
        x, y = outer[0] + point[0], outer[1] + point[1]
        hit = ui.evaluate("([x, y, s]) => document.elementFromPoint(x, y) === document.querySelector(s)",
                          [x, y, ArtifactFrame.OUTER])
        dx, dy = offset(page, ui)
        if hit and ui != page.main_frame:
            hit = page.evaluate("([x, y, s]) => document.elementFromPoint(x, y) === document.querySelector(s)",
                                [x + dx, y + dy, PANEL])
        x, y = x + dx, y + dy
        if not hit:
            raise AssertionError(f"artifact element {self.selector} is covered at ({x:.0f}, {y:.0f})")
        self.frame.page.mouse.click(x, y)


def art(page: Page, ui=None) -> ArtifactFrame:
    return ArtifactFrame(page, ui)


def frame_text(page: Page, selector: str) -> str:
    return art(page).locator(selector).inner_text(timeout=T(10_000))


def host_dispatch(rt: Runtime) -> dict:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HERMES_HOME": str(rt.hermes_home)}
    result = subprocess.run(
        [str(HERMES_PYTHON), str(PROJECT / "scripts" / "host_dispatch.py"), str(FIXTURES), str(rt.root / "work")],
        cwd=HERMES_CHECKOUT, env=env, capture_output=True, text=True, timeout=T(300),
    )
    (rt.root / "logs" / "host-dispatch.err").write_text(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"host dispatch failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


HOST_LUMINANCE = """() => { let n = document.querySelector('.ha-root') || document.body;
    while (n) { const v = getComputedStyle(n).backgroundColor; const p = (v.match(/-?[\\d.]+/g) || []).map(Number);
      const k = v.trim().startsWith('color(') ? 255 : 1;
      if (p.length >= 3 && (p.length < 4 || p[3] > 0.5))
        return (0.2126 * p[0] * k + 0.7152 * p[1] * k + 0.0722 * p[2] * k) / 255;
      n = n.parentElement; }
    return null; }"""


def set_theme(rt: Runtime, name: str) -> None:
    """Operator-level theme for the isolated home; the dashboard reads it on every page load."""
    (rt.hermes_home / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - artifacts\ndashboard:\n  theme: {name}\n")


def settled(page: Page, wait) -> None:
    """Run a wait inside the panel, again on a fresh panel if the dashboard remounted its tab meanwhile."""
    for attempt in range(4):
        try:
            wait(panel(page))
            return
        except Exception as exc:  # noqa: BLE001
            if "detached" not in str(exc) or attempt == 3:
                raise
            page.wait_for_timeout(T(300))


def open_library(page: Page, base: str) -> None:
    page.goto(f"{base}/artifacts")
    page.wait_for_load_state("networkidle")
    settled(page, lambda ui: (
        ui.wait_for_selector(".ha-row, [data-testid=empty-state], .ha-state--error", timeout=T(30_000)),
        ui.wait_for_function("() => !document.querySelector('.ha-library .ha-state--loading')", timeout=T(30_000))))


TOKEN_URL = r"/[A-Za-z0-9_-]{22}"


def open_artifact(page: Page, base: str, slug: str) -> None:
    page.goto(f"{base}/artifacts/{slug}")
    page.wait_for_load_state("networkidle")
    settled(page, lambda ui: ui.wait_for_selector("[data-testid=artifact-frame]", timeout=T(30_000)))
    page.wait_for_timeout(T(600))


TOPMOST = """([selector, inset]) => {
  const target = document.querySelector(selector), r = target.getBoundingClientRect();
  const xs = [r.left + inset, r.left + r.width / 2, r.right - inset], ys = [r.top + inset, r.top + r.height / 2, r.bottom - inset];
  return xs.flatMap(x => ys.map(y => { const hit = document.elementFromPoint(x, y);
    return {x: Math.round(x), y: Math.round(y), covered: !(hit && target.contains(hit)),
            hit: hit ? hit.tagName.toLowerCase() + (hit.className ? '.' + String(hit.className).split(' ')[0] : '') : null}; })); }"""


def topmost(page: Page, selector: str, name: str, viewport_wide: bool = False) -> bool:
    """Hit-tests a 3x3 grid: the element must be what the user actually sees and clicks there.

    Embedded, the same points must also hit the panel iframe on the dashboard page."""
    ui = panel(page)
    points = _topmost_points(ui, selector, viewport_wide)
    if ui != page.main_frame:
        dx, dy = offset(page, ui)
        for point in points:
            on_page = page.evaluate("([x, y, s]) => { const h = document.elementFromPoint(x, y);"
                                    " return h === document.querySelector(s); }", [point["x"] + dx, point["y"] + dy, PANEL])
            if not on_page:
                point["covered"], point["hit"] = True, f"page:{point['hit']}"
    covered = [p for p in points if p["covered"]]
    measurements[name] = {"points": len(points), "covered": covered}
    return check(name, not covered, covered)


def _topmost_points(page, selector: str, viewport_wide: bool) -> list[dict]:
    if viewport_wide:
        points = page.evaluate("""([s, i]) => { const t = document.querySelector(s), out = [];
          for (const x of [i, innerWidth / 2, innerWidth - i, 100]) for (const y of [i, innerHeight / 2, innerHeight - i, 100]) {
            const hit = document.elementFromPoint(x, y); out.push({x: Math.round(x), y: Math.round(y), covered: !(hit && t.contains(hit)),
              hit: hit ? hit.tagName.toLowerCase() + (hit.className ? '.' + String(hit.className).split(' ')[0] : '') : null}); }
          return out; }""", [selector, 4])
    else:
        points = page.evaluate(TOPMOST, [selector, 12])
    return points


def share_round_trip(page: Page, label: str, expected: str, share_tokens: list[str],
                     close_with_escape: bool) -> None:
    """Real clicks: open Share, create a link, revoke it through the confirmation, close; the mode survives."""
    mode = panel(page).get_attribute("[data-testid=viewer]", "data-mode")
    fullscreen = panel(page).evaluate("() => document.fullscreenElement?.dataset.testid ?? null")
    panel(page).click("[data-testid=viewer] .ha-toolbar__actions button:has-text('Share')")
    share = panel(page).locator("dialog.ha-share[open]")
    share.wait_for(timeout=T(10_000))
    page.wait_for_timeout(T(300))
    topmost(page, "dialog.ha-share[open]", f"{label}: share dialog is visible on top")
    check(f"{label}: share dialog takes focus",
          panel(page).evaluate("() => document.querySelector('dialog.ha-share[open]').contains(document.activeElement)"), None)
    shot(page, f"{label}-share-dialog")
    share.locator("button:has-text('Create link')").click()
    share.locator("[data-testid=share-grant]").wait_for(timeout=T(10_000))
    url = share.locator("input[aria-label='Share link']").input_value()
    share_tokens.append(url.rsplit("/", 1)[1])
    check(f"{label}: link carries a 22-character 128-bit token", re.fullmatch(r"http://127\.0\.0\.1:\d+" + TOKEN_URL, url)
          is not None, len(url.rsplit("/", 1)[1]))
    check(f"{label}: created link opens for a guest", http("GET", url)[0] == 200, None)
    check(f"{label}: creation is confirmed inside the dialog",
          share.locator(".ha-share__status").inner_text() == "Share link created.", None)
    shot(page, f"{label}-share-created")
    share.locator(".ha-share__revoke").first.click()  # newest first: the link just created
    confirm = panel(page).locator("dialog.ha-confirm[open]")
    confirm.wait_for(timeout=T(10_000))
    page.wait_for_timeout(T(300))
    topmost(page, "dialog.ha-confirm[open]", f"{label}: revoke confirmation is visible on top")
    shot(page, f"{label}-share-confirm")
    confirm.locator("button:has-text('Revoke')").click()
    panel(page).wait_for_selector("dialog.ha-confirm[open]", state="detached", timeout=T(10_000))
    share.locator(".ha-share__status:has-text('Share link revoked.')").wait_for(timeout=T(10_000))
    check(f"{label}: revoked link no longer opens", http("GET", url)[0] != 200, None)
    if close_with_escape:
        page.keyboard.press("Escape")
    else:
        panel(page).click("dialog.ha-share[open] > .ha-dialog__header button[aria-label=Close]")
    panel(page).wait_for_selector("dialog[open]", state="detached", timeout=T(10_000))
    after = {"mode": panel(page).get_attribute("[data-testid=viewer]", "data-mode"),
             "fullscreen": panel(page).evaluate("() => document.fullscreenElement?.dataset.testid ?? null"),
             "count": frame_text(page, "#count")}
    check(f"{label}: closing sharing keeps the viewer mode and artifact state",
          after == {"mode": mode, "fullscreen": fullscreen, "count": expected}, after)
    topmost(page, "[data-testid=viewer]", f"{label}: viewer covers the window again after sharing", mode != "inline")


def workspace_fill(page: Page, label: str) -> dict:
    view = viewport(page)
    viewer = rect(page, "[data-testid=viewer]")
    frame = rect(page, "[data-testid=artifact-frame]")
    ui = panel(page)
    column = (".ha-root" if ui == page.main_frame else "[data-testid=artifacts-host]")
    container = page.eval_on_selector(column, "e => { let n = e.parentElement;"
                                      " while (n && n.getBoundingClientRect().width === 0) n = n.parentElement;"
                                      " const r = n.getBoundingClientRect(); return {x: r.x, width: r.width}; }")
    available_height = view["height"] - viewer["y"]
    data = {
        "viewport": view, "viewer": viewer, "frame": frame, "content_column": container,
        "frame_height_share_of_available": round(frame["height"] / available_height, 3),
        "frame_width_share_of_column": round(frame["width"] / container["width"], 3),
        "bottom_gap_px": round(view["height"] - (viewer["y"] + viewer["height"]), 1),
    }
    measurements[f"workspace_{label}"] = data
    check(f"{label}: viewer reaches the bottom of the viewport", 0 <= data["bottom_gap_px"] <= 16, data)
    check(f"{label}: frame fills the content column width", data["frame_width_share_of_column"] >= 0.98, data)
    check(f"{label}: frame fills available height below the toolbar",
          data["frame_height_share_of_available"] >= 0.85, data)
    return data


def main() -> int:
    refuse_live(SCRATCH)
    keep = "--keep" in sys.argv
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    SCREENS.mkdir(parents=True)
    root = SCRATCH / "runtime" / "uat"
    if root.exists():
        shutil.rmtree(root)
    rt = Runtime(root)
    host_before = host_status()
    revision = subprocess.run(["git", "-C", str(PROJECT), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(PROJECT), "status", "--porcelain"], capture_output=True,
                                text=True).stdout.strip())
    share_tokens: list[str] = []
    try:
        rt.prepare()
        rt.start_service()
        rt.start_dashboard()
        run(rt, share_tokens)
    except Exception:  # noqa: BLE001 - recorded as a failed gate
        check("uat completed without an unexpected exception", False, traceback.format_exc()[-3000:])
    finally:
        if not keep:
            rt.stop_all()
        secrets_scan(rt, share_tokens)
        check("Hermes checkout unchanged", host_status() == host_before, host_status()[:500])
        report = {
            "revision": revision, "worktree_dirty": dirty, "runtime_root": str(root),
            "service_url": rt.service_url, "dashboard_url": rt.dashboard_url,
            "kept_running": {k: p.popen.pid for k, p in rt.processes.items()} if keep else {},
            "checks": checks, "measurements": measurements,
            "passed": sum(c["ok"] for c in checks), "failed": sum(not c["ok"] for c in checks),
        }
        (EVIDENCE / "report.json").write_text(json.dumps(report, indent=2))
    print(f"UAT: {report['passed']} passed, {report['failed']} failed; evidence {EVIDENCE}")
    return 0 if report["failed"] == 0 and report["passed"] > 0 else 1


def run(rt: Runtime, share_tokens: list[str]) -> None:
    dash, svc = rt.dashboard_url, rt.service_url

    status, _, body = http("GET", f"{dash}/api/dashboard/plugins")
    plugin = next((p for p in json.loads(body) if p["name"] == "artifacts"), None)
    check("dashboard discovers the artifacts plugin page and API",
          plugin is not None and plugin["tab"]["path"] == "/artifacts/*" and plugin["has_api"], plugin)
    for method, path in [("GET", "/api/plugins/artifacts/artifacts"), ("GET", "/api/plugins/artifacts/status"),
                         ("GET", "/api/plugins/artifacts/slugs/orbit-counter"),
                         ("POST", "/api/plugins/artifacts/artifacts/x/shares"),
                         ("DELETE", "/api/plugins/artifacts/shares/x")]:
        code, _, _ = http(method, dash + path, body=b"{}" if method == "POST" else None,
                          headers={"Content-Type": "application/json"})
        check(f"anonymous {method} {path} rejected by Hermes auth", code == 401, code)
    for path in ["/internal/v1/artifacts", "/internal/v1/profiles"]:
        code, _, _ = http("GET", svc + path)
        check(f"service {path} without credential is 401", code == 401, code)
    for path in ["/", "/docs", "/redoc", "/openapi.json", "/debug"]:
        code, _, body = http("GET", svc + path)
        check(f"public {path} exposes nothing", code == 404 and b"Orbit" not in body, code)

    dispatch = host_dispatch(rt)
    share_tokens += [dispatch["share_url"].rsplit("/", 1)[1], dispatch["second_share_url"].rsplit("/", 1)[1]]
    revoked_token = share_tokens[0]
    measurements["host_dispatch"] = {**dispatch, "share_url": "<redacted>", "second_share_url": "<redacted>",
                                     "download": {**dispatch["download"], "path": "<runtime>/work"}}
    check("host Python is 3.11 for plugin discovery", dispatch["python"].startswith("3.11"), dispatch["python"])
    check("plugin loaded under its dynamic host module name",
          "hermes_plugins.artifacts" in dispatch["plugin_modules"], dispatch["plugin_modules"])
    check("six tools registered by real discovery", len(dispatch["loaded_plugins"].get("artifacts", {}).get(
        "tools", [])) == 6, dispatch["loaded_plugins"])
    check("all five formats published privately through host dispatch",
          all(v["visibility"] == "private" and v["id"] for v in dispatch["published"].values()), dispatch["published"])
    check("publishing created no share", dispatch["shares_before_share"] == [], dispatch["shares_before_share"])
    check("download returns original bytes", dispatch["download_matches_fixture"], dispatch["download"])
    check("explicit share defaults to 7 days", dispatch["second_share_expires_in"] == 604_800,
          dispatch["second_share_expires_in"])
    check("revocation through tool works", dispatch["revoked_status"] == "revoked", dispatch["revoked_status"])
    check("format validation refuses a contradictory kind before reading", dispatch["rejected"] == {
        "code": "invalid_arguments", "message": "kind must match the file extension"},
          dispatch["rejected"])
    check("pagination returns disjoint pages", not set(dispatch["list_first_page"]) & set(dispatch["list_second_page"]),
          [dispatch["list_first_page"], dispatch["list_second_page"]])
    ids = {name: info["id"] for name, info in dispatch["published"].items()}
    slugs = {name: info["slug"] for name, info in dispatch["published"].items()}
    check("publish returns a readable slug and its private dashboard path",
          slugs["interactive.html"] == "orbit-counter" and all(
              info["dashboard_path"] == f"/artifacts/{info['slug']}" and "?" not in info["dashboard_path"]
              for info in dispatch["published"].values()), dispatch["published"])
    check("share URLs from tools use 22-character tokens",
          all(re.fullmatch(re.escape(svc) + TOKEN_URL, dispatch[k]) for k in ("share_url", "second_share_url")), None)
    SLUGS.update(slugs)
    for suffix in ("", "/content", "/download"):
        for method in ("GET", "HEAD"):
            code, _, _ = http(method, f"{svc}/{revoked_token}{suffix}")
            check(f"revoked link {method} {suffix or '/'} fails closed", code == 404, code)

    with sync_playwright() as playwright:
        browser = tolerant(playwright.chromium.launch(executable_path=EXECUTABLE))
        measurements["browser"] = {"executable": EXECUTABLE or "bundled", "version": browser.version,
                                   "wait_scale": round(LOAD_SCALE, 2)}
        try:
            dashboard_flows(browser, rt, ids, share_tokens)
            guest_flows(browser, rt, share_tokens)
        finally:
            browser.close()

    lifecycle(rt, ids, share_tokens)


def dashboard_flows(browser, rt: Runtime, ids: dict, share_tokens: list[str]) -> None:
    dash = rt.dashboard_url
    context = browser.new_context(viewport=DESKTOP, color_scheme="dark")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    open_library(page, dash)
    source = page.get_attribute(PANEL, "src") or ""
    check("Hermes hosts the shared panel bundle in an iframe",
          source == f"{dash}/dashboard-plugins/artifacts/dist/console/console.html", source)
    check("the hosted panel sees no Hermes SDK globals", panel(page).evaluate(
        "() => typeof window.__HERMES_PLUGIN_SDK__ + typeof window.__HERMES_PLUGINS__") == "undefinedundefined", None)
    rows = panel(page).locator(".ha-row").count()
    check("library lists all published artifacts", rows >= 6,
          {"rows": rows, "state": panel(page).locator(".ha-state").all_inner_texts()})
    shot(page, "desktop-dark-library")
    panel(page).fill("input[type=search]", "zzz-no-such-artifact")
    panel(page).wait_for_selector("[data-testid=empty-state]", timeout=T(10_000))
    check("filtered empty state is meaningful",
          "No artifacts match" in panel(page).inner_text("[data-testid=empty-state]"), None)
    shot(page, "desktop-dark-empty-filter")
    panel(page).fill("input[type=search]", "")
    panel(page).wait_for_selector(".ha-row")

    panel(page).focus("input[type=search]")
    focused = None
    for _ in range(12):
        page.keyboard.press("Tab")
        focused = panel(page).evaluate("() => ({cls: document.activeElement.className, outline:"
                                " getComputedStyle(document.activeElement).outlineStyle})")
        if "ha-row" in str(focused["cls"]):
            break
    check("keyboard reaches artifact rows with a visible focus ring",
          "ha-row" in str(focused["cls"]) and focused["outline"] != "none", focused)
    shot(page, "desktop-dark-keyboard-focus")
    page.keyboard.press("Enter")
    panel(page).wait_for_selector("[data-testid=artifact-frame]", timeout=T(20_000))
    check("Enter on a focused row opens the viewer at its slug path",
          re.fullmatch(r"/artifacts/[a-z0-9-]+", urlparse(page.url).path) is not None and "a=" not in page.url, page.url)

    page.goto(f"{dash}/artifacts/no-such-artifact")
    page.wait_for_load_state("networkidle")
    missing = panel(page).locator(".ha-state--error").inner_text(timeout=T(15_000))
    check("unknown slug fails closed in the dashboard", "not found" in missing.lower(), missing)
    open_artifact(page, dash, SLUGS["interactive.html"])
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=dash)
    panel(page).click("button:has-text('Copy link')")
    page.wait_for_timeout(T(300))
    copied = page.evaluate("() => navigator.clipboard.readText()")
    check("copied private link is the clean slug URL", copied == f"{dash}/artifacts/{SLUGS['interactive.html']}", copied)
    page.click("nav a:has-text('Artifacts'), aside a:has-text('Artifacts')")
    panel(page).wait_for_selector(".ha-row", timeout=T(10_000))
    try:  # the tab rewrites the host's literal /artifacts/* link as soon as it mounts
        page.wait_for_function("() => location.pathname === '/artifacts'", timeout=T(5_000))
    except Exception:  # noqa: BLE001  reported by the check below
        pass
    check("sidebar Artifacts link returns to the library at /artifacts", urlparse(page.url).path == "/artifacts", page.url)
    open_artifact(page, dash, SLUGS["interactive.html"])
    workspace_fill(page, "desktop-dark")
    shot(page, "desktop-dark-viewer-html")

    frame = art(page)
    external: list[str] = []
    blocked: list[str] = []
    context.on("response", lambda r: external.append(r.url) if "example.com" in r.url else None)
    context.on("requestfailed", lambda r: blocked.append(f"{r.url} {r.failure}") if "example.com" in r.url else None)
    for _ in range(3):
        frame.locator("#increment").click()
    check("interactive HTML runs its script", frame_text(page, "#count") == "3", frame_text(page, "#count"))
    page.wait_for_timeout(T(800))
    probes = json.loads(frame_text(page, "#probes"))
    measurements["dashboard_probes"] = probes
    check("artifact runs in an opaque origin", probes.get("origin") == "null", probes.get("origin"))
    for key in ("cookie", "localStorage", "parentDocument", "topLocation"):
        check(f"artifact cannot access {key}", str(probes.get(key, "")).startswith("blocked"), probes.get(key))
    for key in ("dashboardApi", "managementApi", "externalFetch"):
        check(f"artifact fetch {key} is blocked", str(probes.get(key, "")).startswith("blocked"), probes.get(key))
    check("external image is blocked", probes.get("externalImage") == "blocked", probes.get("externalImage"))
    pages_before = len(context.pages)
    url_before = page.url
    frame.locator("#top-nav").click()
    frame.locator("#popup").click()
    frame.locator("#form button[type=submit]").click()
    page.wait_for_timeout(T(1200))
    probes = json.loads(frame_text(page, "#probes"))
    check("top navigation is blocked", page.url == url_before and str(probes.get("topNavigation")).startswith("blocked"),
          [page.url, probes.get("topNavigation")])
    check("popups are blocked", len(context.pages) == pages_before and str(probes.get("popup")).startswith("blocked"),
          probes.get("popup"))
    check("form submission is blocked", frame_text(page, "#count") == "3" and "formSubmitEvent" not in probes,
          probes.get("formSubmitEvent"))

    frame.locator("#increment").click()
    expected = frame_text(page, "#count")
    panel(page).click("text=Full window")
    panel(page).wait_for_selector("[data-mode=expanded]")
    view, frame_box = viewport(page), rect(page, "[data-testid=artifact-frame]")
    measurements["full_window_desktop"] = {"viewport": view, "frame": frame_box}
    check("full-window mode spans the viewport width", abs(frame_box["width"] - view["width"]) <= 2, frame_box)
    check("full-window frame reaches the viewport bottom",
          abs(frame_box["y"] + frame_box["height"] - view["height"]) <= 2, frame_box)
    check("full-window keeps artifact state", frame_text(page, "#count") == expected, frame_text(page, "#count"))
    topmost(page, "[data-testid=viewer]", "full window covers the host sidebar, header and banners", True)
    shot(page, "desktop-dark-full-window")
    share_round_trip(page, "desktop-dark-full-window", expected, share_tokens, close_with_escape=True)
    page.keyboard.press("Escape")
    panel(page).wait_for_selector("[data-mode=inline]")
    check("Escape restores inline mode with the same state",
          frame_text(page, "#count") == expected and urlparse(page.url).path == f"/artifacts/{SLUGS['interactive.html']}", page.url)
    check("leaving full window gives the host sidebar back",
          page.evaluate("() => !document.querySelector('[data-testid=artifacts-host]').matches(':popover-open')"
                        " && document.elementFromPoint(100, 100)?.closest('aside') !== null"), None)

    panel(page).click("button[aria-label=Fullscreen]")
    panel(page).wait_for_function("() => document.fullscreenElement !== null", timeout=T(10_000))
    panel(page).wait_for_selector("[data-mode=fullscreen]")
    page.wait_for_timeout(T(400))
    view, frame_box = viewport(page), rect(page, "[data-testid=artifact-frame]")
    measurements["fullscreen_desktop"] = {"viewport": view, "frame": frame_box,
                                          "fullscreen_element": panel(page).evaluate(
                                              "() => document.fullscreenElement.dataset.testid")}
    check("Fullscreen API engages on the trusted viewer", measurements["fullscreen_desktop"]["fullscreen_element"]
          == "viewer", measurements["fullscreen_desktop"])
    check("fullscreen frame fills the screen width", abs(frame_box["width"] - view["width"]) <= 2, frame_box)
    check("fullscreen keeps artifact state", frame_text(page, "#count") == expected, frame_text(page, "#count"))
    topmost(page, "[data-testid=viewer]", "fullscreen viewer is what the screen shows", True)
    shot(page, "desktop-dark-fullscreen")
    share_round_trip(page, "desktop-dark-fullscreen", expected, share_tokens, close_with_escape=False)
    panel(page).click("button[aria-label='Exit fullscreen']")
    panel(page).wait_for_function("() => document.fullscreenElement === null", timeout=T(10_000))
    panel(page).wait_for_selector("[data-mode=inline]")
    check("exiting fullscreen preserves selection and state",
          frame_text(page, "#count") == expected and urlparse(page.url).path == f"/artifacts/{SLUGS['interactive.html']}", page.url)
    panel(page).click("button[aria-label='Back to library']")
    panel(page).wait_for_selector(".ha-row", timeout=T(10_000))
    check("library is reachable again after full window and fullscreen", panel(page).locator(".ha-row").count() >= 6, None)
    open_artifact(page, dash, SLUGS["interactive.html"])

    frame.locator("#self-nav").click()
    panel(page).wait_for_selector("text=tried to navigate away", timeout=T(15_000))
    page.wait_for_timeout(T(800))
    srcdoc_restored = art(page).locator("#count").inner_text(timeout=T(10_000))
    check("frame self-navigation is blocked, reported and the artifact restored", srcdoc_restored == "0",
          srcdoc_restored)
    check("no request from the artifact ever reached the external host", external == [], external)
    measurements["dashboard_blocked_external_requests"] = blocked
    shot(page, "desktop-dark-navigation-blocked")

    panel(page).click("button:has-text('Share')")
    panel(page).wait_for_selector("text=Links for this artifact")
    items = panel(page).locator(".ha-share__item").all_inner_texts()
    check("share dialog lists only the earlier revoked links", len(items) == 3
          and all(item.lower().startswith("revoked") for item in items), items)
    topmost(page, "dialog.ha-share[open]", "inline: share dialog is visible on top")
    shot(page, "desktop-dark-share-dialog")
    panel(page).click("button:has-text('Create link')")
    panel(page).wait_for_selector("[data-testid=share-grant]", timeout=T(10_000))
    share_url = panel(page).input_value("input[aria-label='Share link']")
    share_tokens.append(share_url.rsplit("/", 1)[1])
    check("dashboard share returns a guest URL for the service", share_url.startswith(rt.service_url + "/"),
          share_url.split("/")[2])
    check("share creation is confirmed", panel(page).locator("text=Share link created.").count() >= 1, None)
    shot(page, "desktop-dark-share-created")
    measurements["dashboard_share_token"] = "redacted"
    (EVIDENCE / "dashboard-share.txt").write_text("token redacted from evidence\n")
    page.keyboard.press("Escape")
    rt_state = {"html_share_token": share_url.rsplit("/", 1)[1]}
    rt.__dict__["_uat_state"] = rt_state

    for name, label in (("report.md", "markdown"), ("diagram.svg", "svg"), ("chart.png", "png"),
                        ("chart.webp", "webp")):
        open_artifact(page, dash, SLUGS[name])
        fl = art(page)
        if label == "markdown":
            check("markdown renders tables", fl.locator("table").count() == 1, None)
            body = fl.locator("body").inner_text()
            check("markdown raw script is inert text", "pwned" in body and fl.locator("script").count() == 0, None)
            check("markdown javascript: link is neutralized",
                  fl.locator("a[href^='javascript']").count() == 0, None)
        else:
            natural = fl.locator("img").evaluate("img => img.complete ? img.naturalWidth : -1")
            check(f"{label} image renders inside the frame", natural > 0, natural)
            if label == "svg":
                pwned = fl.locator("img").evaluate("img => document.documentElement.getAttribute('data-pwned')")
                check("svg script does not execute", pwned is None, pwned)
        shot(page, f"desktop-dark-viewer-{label}")

    no_fs = browser.new_context(viewport=DESKTOP, color_scheme="dark")
    no_fs.add_init_script("Object.defineProperty(Document.prototype, 'fullscreenEnabled', {get: () => false});"
                          "Element.prototype.requestFullscreen = undefined;")
    fallback_page = no_fs.new_page()
    open_artifact(fallback_page, dash, SLUGS["interactive.html"])
    ff = art(fallback_page)
    ff.locator("#increment").click()
    ff.locator("#increment").click()
    panel(fallback_page).click("button[aria-label=Fullscreen]")
    panel(fallback_page).wait_for_selector("[data-mode=expanded]", timeout=T(10_000))
    view, frame_box = viewport(fallback_page), rect(fallback_page, "[data-testid=artifact-frame]")
    check("without the Fullscreen API the viewer falls back to full window",
          abs(frame_box["width"] - view["width"]) <= 2 and ff.locator("#count").inner_text() == "2",
          {"frame": frame_box, "count": ff.locator("#count").inner_text()})
    fallback_page.wait_for_timeout(T(300))
    topmost(fallback_page, "[data-testid=viewer]", "fullscreen fallback covers the host sidebar and header", True)
    shot(fallback_page, "desktop-dark-fullscreen-fallback")
    fallback_page.set_viewport_size(MOBILE)
    fallback_page.wait_for_timeout(T(300))
    topmost(fallback_page, "[data-testid=viewer]", "390px fullscreen fallback covers the host header", True)
    check("390px fullscreen fallback keeps artifact state", ff.locator("#count").inner_text() == "2", None)
    shot(fallback_page, "mobile-dark-fullscreen-fallback")
    share_round_trip(fallback_page, "mobile-dark-fullscreen-fallback", "2", share_tokens,
                     close_with_escape=False)
    no_fs.close()

    mobile = browser.new_context(viewport=MOBILE, color_scheme="dark", is_mobile=True, has_touch=True,
                                 device_scale_factor=2)
    mpage = mobile.new_page()
    open_library(mpage, dash)
    shot(mpage, "mobile-dark-library")
    overflow = panel(mpage).evaluate("() => document.documentElement.scrollWidth - innerWidth")
    check("mobile library has no horizontal overflow", overflow <= 1, overflow)
    open_artifact(mpage, dash, SLUGS["interactive.html"])
    data = workspace_fill(mpage, "mobile-dark")
    check("mobile frame keeps at least 85% of the viewport width",
          data["frame"]["width"] >= 0.85 * MOBILE["width"], data["frame"])
    shot(mpage, "mobile-dark-viewer")
    panel(mpage).click("button[aria-label=Fullscreen]")
    panel(mpage).wait_for_function("() => document.fullscreenElement !== null || document.querySelector('[data-mode=expanded]')")
    mpage.wait_for_timeout(T(400))
    box = rect(mpage, "[data-testid=artifact-frame]")
    check("mobile fullscreen uses the full width", abs(box["width"] - MOBILE["width"]) <= 2, box)
    topmost(mpage, "[data-testid=viewer]", "mobile fullscreen covers the host header", True)
    shot(mpage, "mobile-dark-fullscreen")
    mobile.close()

    light = browser.new_context(viewport=DESKTOP, color_scheme="light")
    set_theme(rt, "nous-blue")
    lpage = light.new_page()
    open_library(lpage, dash)
    lpage.wait_for_function(f"({HOST_LUMINANCE})() > 0.5", timeout=T(10_000))
    luminance = lpage.evaluate(HOST_LUMINANCE)
    check("light Hermes theme is active behind the plugin", luminance > 0.5, luminance)
    shot(lpage, "desktop-light-library")
    open_artifact(lpage, dash, SLUGS["report.md"])
    workspace_fill(lpage, "desktop-light")
    doc_scheme = art(lpage).locator("html").evaluate(
        "h => getComputedStyle(h).colorScheme")
    check("markdown preview follows the light host theme", doc_scheme == "light", doc_scheme)
    shot(lpage, "desktop-light-viewer-markdown")
    open_artifact(lpage, dash, SLUGS["interactive.html"])
    shot(lpage, "desktop-light-viewer-html")
    lmobile = browser.new_context(viewport=MOBILE, color_scheme="light", is_mobile=True, has_touch=True,
                                  device_scale_factor=2)
    lm = lmobile.new_page()
    open_artifact(lm, dash, SLUGS["chart.webp"])
    lm.wait_for_function(f"({HOST_LUMINANCE})() > 0.5", timeout=T(10_000))
    shot(lm, "mobile-light-viewer-webp")
    lmobile.close()
    set_theme(rt, "default")
    light.close()

    check("no uncaught page errors in the dashboard", not errors, errors[:3])
    context.close()


def guest_flows(browser, rt: Runtime, share_tokens: list[str]) -> None:
    svc = rt.service_url
    token = rt.__dict__["_uat_state"]["html_share_token"]
    for scheme, size, label in (("dark", DESKTOP, "desktop-dark"), ("light", DESKTOP, "desktop-light"),
                                ("dark", MOBILE, "mobile-dark"), ("light", MOBILE, "mobile-light")):
        context = browser.new_context(viewport=size, color_scheme=scheme, is_mobile=size is MOBILE)
        page = context.new_page()
        response = page.goto(f"{svc}/{token}")
        page.wait_for_timeout(T(900))
        view, frame_box = viewport(page), rect(page, "#artifact")
        measurements[f"guest_{label}"] = {"viewport": view, "frame": frame_box}
        check(f"guest {label}: shell responds 200 with no-store",
              response.status == 200 and response.headers.get("cache-control") == "no-store", response.status)
        check(f"guest {label}: artifact frame covers the entire viewport",
              frame_box == {"x": 0, "y": 0, "width": view["width"], "height": view["height"]}, frame_box)
        gf = page.frame_locator("#artifact")
        external: list[str] = []
        blocked: list[str] = []
        context.on("response", lambda r, seen=external: seen.append(r.url) if "example.com" in r.url else None)
        context.on("requestfailed",
                   lambda r, seen=blocked: seen.append(f"{r.url} {r.failure}") if "example.com" in r.url else None)
        if label == "desktop-dark":
            gf.locator("#increment").click()
            check("guest artifact script runs", gf.locator("#count").inner_text() == "1", None)
            page.wait_for_timeout(T(600))
            probes = json.loads(gf.locator("#probes").inner_text())
            measurements["guest_probes"] = probes
            check("guest artifact origin is opaque", probes.get("origin") == "null", probes.get("origin"))
            check("guest artifact cannot fetch management API",
                  str(probes.get("managementApi")).startswith("blocked"), probes.get("managementApi"))
            check("guest artifact cannot fetch external URLs",
                  str(probes.get("externalFetch")).startswith("blocked"), probes.get("externalFetch"))
            check("guest artifact cannot read its parent", str(probes.get("parentDocument")).startswith("blocked"),
                  probes.get("parentDocument"))
            pages_before, url_before = len(context.pages), page.url
            gf.locator("#top-nav").click()
            gf.locator("#popup").click()
            page.wait_for_timeout(T(800))
            check("guest top navigation and popups are blocked",
                  page.url == url_before and len(context.pages) == pages_before, page.url)
            page.click("#menu-toggle")
            check("guest menu explains expiry and forwarding and offers Download",
                  "Anyone with the link" in page.inner_text("#menu")
                  and page.get_attribute("#menu a[download]", "href") == f"/{token}/download", None)
            shot(page, "guest-desktop-dark-menu")
            page.keyboard.press("Escape")
            check("guest menu closes on Escape and keeps artifact state",
                  page.is_hidden("#menu") and gf.locator("#count").inner_text() == "1", None)
            gf.locator("#self-nav").click()
            page.wait_for_selector("#notice:not([hidden])", timeout=T(15_000))
            check("guest frame navigation is detected and reloaded", "navigate away" in page.inner_text("#notice"),
                  page.inner_text("#notice"))
            check("guest artifact never reached the external host", external == [], external)
            measurements["guest_blocked_external_requests"] = blocked
            direct = context.new_page()
            direct.goto(f"{svc}/{token}/content")
            check("opening /content top-level lands on the protective shell",
                  direct.url == f"{svc}/{token}" and direct.locator("#artifact").count() == 1, direct.url)
            direct.close()
            headers = response.headers
            check("guest shell sends strict CSP and no-referrer",
                  "frame-ancestors 'none'" in headers.get("content-security-policy", "")
                  and headers.get("referrer-policy") == "no-referrer", headers.get("content-security-policy"))
        shot(page, f"guest-{label}")
        context.close()

    code, headers, body = http("GET", f"{svc}/{token}/download")
    check("guest download is an attachment with original bytes",
          code == 200 and headers.get("content-disposition", "").startswith("attachment")
          and body == (FIXTURES / "interactive.html").read_bytes(), headers.get("content-disposition"))

    page = browser.new_page(viewport=DESKTOP)
    page.goto(f"{svc}/{'A' * 22}")
    check("unknown guest link shows the unavailable page", "This link is unavailable" in page.inner_text("h1"), None)
    shot(page, "guest-unavailable")
    page.close()


def lifecycle(rt: Runtime, ids: dict, share_tokens: list[str]) -> None:
    svc = rt.service_url
    token = rt.__dict__["_uat_state"]["html_share_token"]
    code, _, body = http("POST", f"{svc}/internal/v1/artifacts/{ids['report.md']}/shares", rt.token,
                         json.dumps({"expires_in_seconds": 2}).encode(), {"Content-Type": "application/json"})
    short = json.loads(body)["url"].rsplit("/", 1)[1]
    share_tokens.append(short)
    check("short-lived share works before expiry", http("GET", f"{svc}/{short}/content")[0] == 200, code)
    started = time.monotonic()
    expired_after = None
    while time.monotonic() - started < 8:
        if http("GET", f"{svc}/{short}/content")[0] == 404:
            expired_after = round(time.monotonic() - started, 2)
            break
        time.sleep(0.25)
    check("short-lived share expires on the real server within a bounded wait", expired_after is not None,
          expired_after)
    measurements["short_share_expired_after_seconds"] = expired_after
    for suffix in ("", "/content", "/download"):
        check(f"expired link HEAD {suffix or '/'} fails closed", http("HEAD", f"{svc}/{short}{suffix}")[0] == 404, None)

    count_before = len(json.loads(http("GET", f"{svc}/internal/v1/artifacts?limit=100", rt.token)[2])["items"])
    rt.stop("service")
    check("service is down after stop", _down(f"{svc}/healthz"), None)
    with sync_playwright() as playwright:
        browser = tolerant(playwright.chromium.launch(executable_path=EXECUTABLE))
        page = browser.new_page(viewport=DESKTOP)
        open_library(page, rt.dashboard_url)
        alert = panel(page).locator(".ha-state--error").inner_text(timeout=T(15_000))
        check("dashboard shows a useful error when the service is unreachable", "unreachable" in alert, alert)
        shot(page, "desktop-dark-service-unreachable")
        browser.close()
    rt.start_service()
    count_after = len(json.loads(http("GET", f"{svc}/internal/v1/artifacts?limit=100", rt.token)[2])["items"])
    check("artifacts survive service restart", count_after == count_before, [count_before, count_after])
    check("active share survives service restart", http("GET", f"{svc}/{token}")[0] == 200, None)

    backup = rt.root / "backup" / "artifacts-backup.db"
    result = subprocess.run([str(SERVICE_PYTHON), "-m", "app.backup", str(backup)], cwd=PROJECT, capture_output=True,
                            text=True, env={"PATH": "/usr/bin:/bin", "ARTIFACTS_DB": str(rt.database)})
    check("online backup runs while the service is live", result.returncode == 0, result.stderr[-500:])
    restore_port = free_port()
    restored = rt.start_service(database=backup, port=restore_port, name="restored")
    restored_items = json.loads(http("GET", f"{restored}/internal/v1/artifacts?limit=100", rt.token)[2])["items"]
    check("backup restores all artifacts into an isolated service", len(restored_items) == count_before,
          len(restored_items))
    check("backup restores active shares", http("GET", f"{restored}/{token}/content")[0] == 200, None)
    check("backup preserves revoked and expired state",
          http("GET", f"{restored}/{short}")[0] == 404, None)
    rt.stop("restored")

    rt.stop("dashboard")
    check("guest share works while Hermes is stopped", http("GET", f"{svc}/{token}")[0] == 200
          and http("GET", f"{svc}/{token}/content")[0] == 200, None)
    rt.start_dashboard()
    code, _, _ = http("GET", f"{rt.dashboard_url}/api/dashboard/plugins")
    check("Hermes dashboard restarts with the plugin", code == 200, code)
    check("guest share still works after Hermes restart", http("GET", f"{svc}/{token}")[0] == 200, None)


def _down(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=T(2))
        return False
    except Exception:  # noqa: BLE001
        return True


def secrets_scan(rt: Runtime, share_tokens: list[str]) -> None:
    needles = [rt.token, *share_tokens]
    hits = []
    for folder in (rt.root / "logs", EVIDENCE):
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix != ".png":
                text = path.read_text(errors="ignore")
                hits += [str(path) for needle in needles if needle and needle in text]
    check("no service credential or share token in logs or evidence", not hits, hits[:5])
    tracked = subprocess.run(["git", "-C", str(PROJECT), "grep", "-l", rt.token], capture_output=True, text=True)
    check("service credential is not in the repository", tracked.stdout.strip() == "", tracked.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
