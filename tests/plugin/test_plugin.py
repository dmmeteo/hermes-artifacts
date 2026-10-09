"""Plugin behavior under the host runtime (Hermes venv, Python 3.11).

The service runs for real as a subprocess under its own Python 3.14 venv. The
dashboard router is imported by file path, the way Hermes mounts it.
"""

import re
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT = Path(__file__).resolve().parents[2]
TOKEN = "p" * 40
sys.path.insert(0, str(PROJECT / "scripts"))
from runtime import SERVICE_PYTHON, free_port  # noqa: E402


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    port = free_port()
    root = tmp_path_factory.mktemp("service")
    env = {"PATH": os.environ["PATH"], "ARTIFACTS_DB": str(root / "a.db"), "ARTIFACTS_API_TOKEN": TOKEN,
           "ARTIFACTS_PORT": str(port), "ARTIFACTS_PUBLIC_URL": "https://art.example.test"}
    process = subprocess.Popen([str(SERVICE_PYTHON), "-m", "app"], cwd=PROJECT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield url
    process.terminate()
    process.wait(10)


@pytest.fixture
def configured(service, monkeypatch):
    monkeypatch.setenv("HERMES_ARTIFACTS_URL", service)
    monkeypatch.setenv("HERMES_ARTIFACTS_TOKEN", TOKEN)
    return service


def load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location("hermes_plugins_test_artifacts", PROJECT / "plugin" / "__init__.py",
                                                  submodule_search_locations=[str(PROJECT / "plugin")])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tools(plugin):
    return {name: handler for name, handler, _, _ in plugin.tools.TOOLS}


@pytest.fixture
def dashboard(configured):
    module = load_by_path("hermes_dashboard_plugin_artifacts", PROJECT / "plugin" / "dashboard" / "plugin_api.py")
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/artifacts")
    return TestClient(app)


def call(tools, name, args):
    return json.loads(tools[name](args, session_id="session-1"))


def test_runs_on_host_python():
    assert sys.version_info[:2] == (3, 11)


def test_registers_the_packaged_operating_skill(plugin):
    skills = []

    class Ctx:
        def register_tool(self, **kwargs):
            pass

        def register_skill(self, name, path, description=""):
            skills.append((name, Path(path), description))

    plugin.register(Ctx())
    assert [(name, path.name) for name, path, _ in skills] == [("operating", "SKILL.md")]
    text = skills[0][1].read_text()
    assert skills[0][1].is_file() and text.startswith("---\nname: operating\n")
    for topic in ("artifact_share", "artifact_revoke_share", "next_cursor", "source_profile", "not_configured",
                  "immutable", "Never print it"):
        assert topic in text


def test_tool_descriptions_point_to_the_operating_skill(plugin):
    described = {name: schema["description"] for name, _, _, schema in plugin.TOOLS}
    assert "skill_view('artifacts:operating')" in described["artifact_publish"]
    assert "skill_view('artifacts:operating')" in described["artifact_share"]


def test_registers_six_tools_in_one_toolset(plugin):
    registered = []

    class Ctx:
        def register_tool(self, **kwargs):
            registered.append((kwargs["name"], kwargs["toolset"], kwargs["check_fn"]))

    plugin.register(Ctx())
    assert [r[0] for r in registered] == ["artifact_publish", "artifact_list", "artifact_get", "artifact_download",
                                          "artifact_share", "artifact_revoke_share"]
    assert {r[1] for r in registered} == {"artifacts"}


def test_tools_report_missing_configuration(tools, monkeypatch):
    monkeypatch.delenv("HERMES_ARTIFACTS_URL", raising=False)
    monkeypatch.delenv("HERMES_ARTIFACTS_TOKEN", raising=False)
    assert call(tools, "artifact_list", {})["error"]["code"] == "not_configured"


def test_publish_requires_exactly_one_source(tools, configured, tmp_path):
    both = call(tools, "artifact_publish", {"title": "x", "path": str(tmp_path / "a.md"), "content": "# x",
                                            "kind": "markdown"})
    assert both["error"]["message"] == "provide exactly one of 'path' or 'content'"
    image_inline = call(tools, "artifact_publish", {"title": "x", "content": "abc", "kind": "png"})
    assert image_inline["error"]["message"] == "inline content needs kind html, markdown or svg; publish images by path"
    unknown = tmp_path / "notes.txt"
    unknown.write_text("x")
    assert call(tools, "artifact_publish", {"title": "x", "path": str(unknown)})["error"]["message"] == (
        "cannot infer kind from the file extension; pass kind")


def test_publish_share_revoke_roundtrip(tools, configured, tmp_path):
    source = tmp_path / "Weekly Report.md"
    source.write_text("# Weekly\n\nAll good.")
    published = call(tools, "artifact_publish", {"path": str(source), "title": ""})
    artifact = published["artifact"]
    assert (artifact["title"], artifact["kind"], published["visibility"]) == ("Weekly Report", "markdown", "private")
    assert artifact["source_session_id"] == "session-1"
    assert artifact["slug"] == "weekly-report" and published["dashboard_path"] == "/artifacts/weekly-report"
    detail = call(tools, "artifact_get", {"artifact_id": artifact["id"]})
    assert detail["shares"] == [] and detail["text_preview"] == "# Weekly\n\nAll good."
    shared = call(tools, "artifact_share", {"artifact_id": artifact["id"], "expires_in_hours": 1})
    assert shared["share"]["expires_at"] - shared["share"]["created_at"] == 3600
    assert re.fullmatch(r"https://art\.example\.test/[A-Za-z0-9_-]{22}", shared["url"])
    revoked = call(tools, "artifact_revoke_share", {"share_id": shared["share"]["id"]})
    assert revoked["share"]["status"] == "revoked"
    target = tmp_path / "out" / "copy.md"
    downloaded = call(tools, "artifact_download", {"artifact_id": artifact["id"], "path": str(target)})
    assert target.read_text() == "# Weekly\n\nAll good." and downloaded["byte_length"] == 19
    again = call(tools, "artifact_download", {"artifact_id": artifact["id"], "path": str(target)})
    assert again["error"]["message"] == f"{target} exists; pass overwrite=true to replace it"


def test_service_errors_surface_to_the_agent(tools, configured, monkeypatch):
    missing = call(tools, "artifact_get", {"artifact_id": "00000000-0000-4000-8000-000000000000"})
    assert missing == {"error": {"code": "not_found", "message": "artifact not found"}}
    monkeypatch.setenv("HERMES_ARTIFACTS_TOKEN", "wrong-token-value")
    assert call(tools, "artifact_list", {})["error"]["code"] == "unauthorized"
    monkeypatch.setenv("HERMES_ARTIFACTS_URL", "http://127.0.0.1:9")
    assert call(tools, "artifact_list", {})["error"]["code"] == "service_unreachable"


def test_a_broken_http_reply_is_reported_not_raised(tools, monkeypatch):
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def answer():
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"NOT-HTTP\r\n\r\n")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    monkeypatch.setenv("HERMES_ARTIFACTS_URL", f"http://127.0.0.1:{server.getsockname()[1]}")
    monkeypatch.setenv("HERMES_ARTIFACTS_TOKEN", TOKEN)
    try:
        assert call(tools, "artifact_list", {})["error"]["code"] == "service_unreachable"
    finally:
        server.close()


def test_dashboard_routes_forward_and_never_return_markup_as_html(dashboard, tools):
    published = call(tools, "artifact_publish", {"content": "<script>parent.x=1</script>", "kind": "html",
                                                 "title": "Script"})
    artifact_id = published["artifact"]["id"]
    listing = dashboard.get("/api/plugins/artifacts/artifacts", params={"kind": "html"}).json()
    assert artifact_id in [a["id"] for a in listing["items"]]
    document = dashboard.get(f"/api/plugins/artifacts/artifacts/{artifact_id}/document", params={"theme": "dark"})
    assert document.headers["content-type"] == "application/json"
    assert "<script>parent.x=1</script>" in document.json()["html"]
    assert "frame-src 'none'" in document.json()["policy"]
    download = dashboard.get(f"/api/plugins/artifacts/artifacts/{artifact_id}/download")
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"] == 'attachment; filename="Script.html"'
    assert download.headers["content-security-policy"].startswith("sandbox")
    share = dashboard.post(f"/api/plugins/artifacts/artifacts/{artifact_id}/shares", json={"expires_in_seconds": 60})
    assert share.status_code == 201
    revoke = dashboard.delete(f"/api/plugins/artifacts/shares/{share.json()['share']['id']}")
    assert revoke.json()["share"]["status"] == "revoked"
    assert dashboard.get("/api/plugins/artifacts/artifacts/not-a-uuid").status_code == 404
    by_slug = dashboard.get(f"/api/plugins/artifacts/slugs/{published['artifact']['slug']}").json()
    assert by_slug["artifact"]["id"] == artifact_id and published["dashboard_path"] == "/artifacts/script"
    for slug in ("missing", "Script", "..%2Fartifacts", artifact_id.upper()):
        assert dashboard.get(f"/api/plugins/artifacts/slugs/{slug}").status_code == 404
    bad = dashboard.post(f"/api/plugins/artifacts/artifacts/{artifact_id}/shares", json={"expires_in_seconds": "1"})
    assert bad.status_code == 422
    implicit = dashboard.post(f"/api/plugins/artifacts/artifacts/{artifact_id}/shares", json={})
    assert implicit.status_code == 422
    assert dashboard.get("/api/plugins/artifacts/status").json() == {"configured": True, "reachable": True}


def test_dashboard_maps_service_auth_failure_away_from_401(dashboard, monkeypatch):
    monkeypatch.setenv("HERMES_ARTIFACTS_TOKEN", "wrong-token-value")
    response = dashboard.get("/api/plugins/artifacts/artifacts")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "service_auth_failed"
    assert "wrong-token-value" not in response.text


def test_dashboard_reports_unconfigured_state(dashboard, monkeypatch):
    monkeypatch.delenv("HERMES_ARTIFACTS_URL")
    assert dashboard.get("/api/plugins/artifacts/artifacts").status_code == 503
    assert dashboard.get("/api/plugins/artifacts/status").json() == {"configured": False, "reachable": False}
