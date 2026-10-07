"""Real host E2E: Hermes discovers the installed plugin and dispatches its tools.

Run with the Hermes venv Python and HERMES_HOME pointing at an isolated home
where the plugin is installed. Prints one JSON report on stdout.
Usage: python host_dispatch.py FIXTURES_DIR WORK_DIR
"""

import json
import platform
import sys
from pathlib import Path

fixtures, work = Path(sys.argv[1]), Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402
from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402

load_hermes_dotenv(load_external_secrets=False)

discover_plugins()
import model_tools  # noqa: E402
from tools.registry import registry  # noqa: E402

manager = get_plugin_manager()
loaded = {key: plugin for key, plugin in manager._plugins.items() if plugin.manifest.name == "artifacts"}
module_names = sorted(name for name in sys.modules if "artifacts" in name and "plugin" in name)
definitions = model_tools.get_tool_definitions(quiet_mode=True)
offered = sorted(d["function"]["name"] for d in definitions if d["function"]["name"].startswith("artifact_"))


def call(name, args):
    raw = model_tools.handle_function_call(name, args, task_id="host-e2e", session_id="host-e2e-session")
    return json.loads(raw)


published = {}
for filename, title in [
    ("interactive.html", "Orbit counter"),
    ("report.md", "Quarterly delivery review"),
    ("diagram.svg", "Architecture sketch"),
    ("chart.png", "Weekly chart (PNG)"),
    ("chart.webp", "Weekly chart (WebP)"),
]:
    result = call("artifact_publish", {"path": str(fixtures / filename), "title": title})
    published[filename] = result

inline = call("artifact_publish", {"content": "# Inline note\n\nPublished from content.", "kind": "markdown",
                                   "title": "Inline note"})
rejected = call("artifact_publish", {"path": str(fixtures / "report.md"), "kind": "png", "title": "Wrong kind"})
listing = call("artifact_list", {"limit": 3})
page_two = call("artifact_list", {"limit": 3, "cursor": listing.get("next_cursor")})
only_png = call("artifact_list", {"kind": "png"})
html_id = published["interactive.html"]["artifact"]["id"]
detail_before = call("artifact_get", {"artifact_id": html_id})
downloaded = call("artifact_download", {"artifact_id": published["chart.webp"]["artifact"]["id"],
                                        "path": str(work / "chart-roundtrip.webp"), "overwrite": True})
shared = call("artifact_share", {"artifact_id": html_id, "expires_in_hours": 24})
detail_shared = call("artifact_get", {"artifact_id": html_id})
revoked = call("artifact_revoke_share", {"share_id": shared["share"]["id"]})
second_share = call("artifact_share", {"artifact_id": published["report.md"]["artifact"]["id"]})
missing = call("artifact_get", {"artifact_id": "00000000-0000-4000-8000-000000000000"})

report = {
    "python": platform.python_version(),
    "loaded_plugins": {key: {"enabled": p.enabled, "tools": sorted(p.tools_registered)} for key, p in loaded.items()},
    "plugin_modules": module_names,
    "toolset": sorted({registry.get_entry(n).toolset for n in offered}),
    "offered_tools": offered,
    "published": {k: {"id": v.get("artifact", {}).get("id"), "slug": v.get("artifact", {}).get("slug"),
                      "dashboard_path": v.get("dashboard_path"), "kind": v.get("artifact", {}).get("kind"),
                      "visibility": v.get("visibility"), "error": v.get("error")} for k, v in published.items()},
    "inline": inline.get("artifact", {}).get("kind"),
    "rejected": rejected.get("error"),
    "list_first_page": [a["title"] for a in listing.get("items", [])],
    "list_second_page": [a["title"] for a in page_two.get("items", [])],
    "only_png": [a["title"] for a in only_png.get("items", [])],
    "shares_before_share": detail_before.get("shares"),
    "text_preview_start": detail_before.get("text_preview", "")[:40],
    "download": downloaded,
    "download_matches_fixture": (work / "chart-roundtrip.webp").read_bytes() == (fixtures / "chart.webp").read_bytes(),
    "share_url": shared.get("url"),
    "share_expires_in": shared["share"]["expires_at"] - shared["share"]["created_at"],
    "share_status_after_share": [s["status"] for s in detail_shared.get("shares", [])],
    "revoked_status": revoked.get("share", {}).get("status"),
    "second_share_url": second_share.get("url"),
    "second_share_expires_in": second_share["share"]["expires_at"] - second_share["share"]["created_at"],
    "missing_error": missing.get("error"),
    "source_profile": published["interactive.html"]["artifact"].get("source_profile"),
    "source_session_id": published["interactive.html"]["artifact"].get("source_session_id"),
}
print(json.dumps(report))
