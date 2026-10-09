import base64
import os
import logging
import re
import sqlite3
import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.domain import Kind
from app.store import Store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FRAME = {"Sec-Fetch-Dest": "iframe"}


def png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


WEBP = base64.b64decode("UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAwA0JaQAA3AA/vuUAAA=")


class Clock:
    def __init__(self, start: int = 1_800_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path: Path):
    return Settings(database=tmp_path / "a.db", api_token=TOKEN, public_url="https://art.example.test",
                    max_bytes=64 * 1024)


@pytest.fixture
def client(settings, clock):
    return TestClient(create_app(settings, clock))


def publish(client, content: bytes, kind: str, title: str = "Report", **params):
    response = client.post(
        "/internal/v1/artifacts", params={"title": title, "kind": kind, **params}, content=content, headers=AUTH
    )
    assert response.status_code == 201, response.text
    return response.json()


def share(client, artifact_id: str, seconds: int | None = None):
    body = {} if seconds is None else {"expires_in_seconds": seconds}
    response = client.post(f"/internal/v1/artifacts/{artifact_id}/shares", json=body, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def test_management_rejects_missing_and_wrong_credentials(client):
    assert client.get("/internal/v1/artifacts").status_code == 401
    wrong = client.get("/internal/v1/artifacts", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": {"code": "unauthorized", "message": "a valid service credential is required"}}
    upload = client.post("/internal/v1/artifacts", params={"title": "x", "kind": "html"}, content=b"<p>x</p>")
    assert upload.status_code == 401


def test_public_surface_has_no_listing_docs_or_schema(client):
    publish(client, b"<p>secret</p>", "html", title="Secret plan")
    for path in ["/", "/docs", "/redoc", "/openapi.json", "/internal/v1/artifacts"]:
        response = client.get(path)
        assert response.status_code in (401, 404), path
        assert "Secret plan" not in response.text


def test_publish_keeps_original_bytes_and_creates_no_share(client):
    html = b"<!doctype html><button onclick=\"this.textContent++\">0</button>"
    artifact = publish(client, html, "html", title="Counter", source_profile="prime", source_session_id="s-1")
    assert artifact["kind"] == "html"
    assert artifact["byte_length"] == len(html)
    assert artifact["active_shares"] == 0
    detail = client.get(f"/internal/v1/artifacts/{artifact['id']}", headers=AUTH).json()
    assert detail["shares"] == []
    download = client.get(f"/internal/v1/artifacts/{artifact['id']}/download", headers=AUTH)
    assert download.content == html
    assert download.headers["content-disposition"] == 'attachment; filename="Counter.html"'


@pytest.mark.parametrize(
    ("kind", "content", "message"),
    [
        ("png", b"not a png", "content is not a PNG image"),
        ("webp", b"RIFF0000WAVE", "content is not a WebP image"),
        ("svg", b"<html></html>", "content is not an SVG document"),
        ("markdown", b"\xff\xfe", "markdown content must be UTF-8 text"),
        ("pdf", b"%PDF", "kind must be one of: html, markdown, svg, png, webp"),
        ("html", b"", "content is empty"),
    ],
)
def test_publish_rejects_invalid_payloads(client, kind, content, message):
    response = client.post(
        "/internal/v1/artifacts", params={"title": "x", "kind": kind}, content=content, headers=AUTH
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == message


def test_publish_enforces_size_limit(client):
    response = client.post(
        "/internal/v1/artifacts", params={"title": "big", "kind": "html"}, content=b"a" * (64 * 1024 + 1),
        headers=AUTH,
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_artifacts_are_immutable_in_storage(client, settings):
    artifact = publish(client, b"# One", "markdown")
    with sqlite3.connect(settings.database) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE artifacts SET content = ? WHERE id = ?", (b"# Two", artifact["id"]))


def test_list_paginates_and_filters_metadata(client, clock):
    for index in range(5):
        clock.now += 1
        publish(client, f"# Note {index}".encode(), "markdown", title=f"Note {index}",
                source_profile="prime" if index % 2 else "ops")
    clock.now += 1
    publish(client, png_bytes(), "png", title="Chart 100%")
    first = client.get("/internal/v1/artifacts", params={"limit": 4}, headers=AUTH).json()
    assert [a["title"] for a in first["items"]] == ["Chart 100%", "Note 4", "Note 3", "Note 2"]
    second = client.get(
        "/internal/v1/artifacts", params={"limit": 4, "cursor": first["next_cursor"]}, headers=AUTH
    ).json()
    assert [a["title"] for a in second["items"]] == ["Note 1", "Note 0"]
    assert second["next_cursor"] is None
    assert "content" not in first["items"][0]
    by_kind = client.get("/internal/v1/artifacts", params={"kind": "png"}, headers=AUTH).json()
    assert [a["title"] for a in by_kind["items"]] == ["Chart 100%"]
    by_profile = client.get("/internal/v1/artifacts", params={"profile": "prime"}, headers=AUTH).json()
    assert [a["title"] for a in by_profile["items"]] == ["Note 3", "Note 1"]
    literal_percent = client.get("/internal/v1/artifacts", params={"q": "100%"}, headers=AUTH).json()
    assert [a["title"] for a in literal_percent["items"]] == ["Chart 100%"]
    wildcard = client.get("/internal/v1/artifacts", params={"q": "%"}, headers=AUTH).json()
    assert [a["title"] for a in wildcard["items"]] == ["Chart 100%"]
    assert client.get("/internal/v1/profiles", headers=AUTH).json() == {"profiles": ["ops", "prime", "unknown"]}
    bad = client.get("/internal/v1/artifacts", params={"cursor": "zzz"}, headers=AUTH)
    assert bad.status_code == 422


def test_share_defaults_to_seven_days_and_grants_one_artifact(client, clock):
    shared = publish(client, b"<p>shared</p>", "html", title="Shared")
    other = publish(client, b"<p>other</p>", "html", title="Other")
    grant = share(client, shared["id"])
    assert grant["share"]["expires_at"] - grant["share"]["created_at"] == 7 * 86_400
    assert grant["url"].startswith("https://art.example.test/")
    token = token_of(grant["url"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", token) and len(base64.urlsafe_b64decode(token + "==")) == 16
    assert token not in (shared["id"], shared["slug"], shared["content_sha256"])
    shell = client.get(f"/{token}")
    assert shell.status_code == 200
    assert "Shared" in shell.text and "Other" not in shell.text
    content = client.get(f"/{token}/content", headers=FRAME)
    assert "<p>shared</p>" in content.text
    assert "sandbox allow-scripts" in content.headers["content-security-policy"]
    assert "connect-src 'none'" in content.headers["content-security-policy"]
    assert client.get(f"/{token}/download").content == b"<p>shared</p>"
    assert other["id"] not in shell.text


def test_expired_revoked_unknown_and_malformed_links_fail_closed(client, clock):
    artifact = publish(client, b"<p>x</p>", "html")
    expiring = token_of(share(client, artifact["id"], 60)["url"])
    revoked_grant = share(client, artifact["id"])
    revoked = token_of(revoked_grant["url"])
    assert client.get(f"/{expiring}/content", headers=FRAME).status_code == 200
    clock.now += 60
    assert client.delete(f"/internal/v1/shares/{revoked_grant['share']['id']}", headers=AUTH).status_code == 200
    unknown = "A" * 22
    malformed = "not-a-token"
    old_format = "0" * 64
    slug = artifact["slug"]
    reference = client.get(f"/{unknown}")
    for token in (expiring, revoked, unknown, malformed, old_format, slug, artifact["id"]):
        for suffix in ("", "/content", "/download"):
            for method in ("GET", "HEAD"):
                response = client.request(method, f"/{token}{suffix}", headers=FRAME)
                assert response.status_code == 404, (token, suffix, method)
                assert response.headers["cache-control"] == "no-store"
                if method == "GET":
                    assert response.text == reference.text
    statuses = {s["id"]: s["status"] for s in client.get(
        f"/internal/v1/artifacts/{artifact['id']}/shares", headers=AUTH).json()["shares"]}
    assert sorted(statuses.values()) == ["expired", "revoked"]


def test_revocation_is_per_share_and_idempotent(client):
    artifact = publish(client, b"<p>x</p>", "html")
    first, second = share(client, artifact["id"]), share(client, artifact["id"])
    revoked = client.delete(f"/internal/v1/shares/{first['share']['id']}", headers=AUTH).json()["share"]
    again = client.delete(f"/internal/v1/shares/{first['share']['id']}", headers=AUTH).json()["share"]
    assert revoked["status"] == again["status"] == "revoked"
    assert revoked["revoked_at"] == again["revoked_at"]
    assert client.get(f"/{token_of(second['url'])}").status_code == 200
    assert client.delete("/internal/v1/shares/nope", headers=AUTH).status_code == 404


def test_share_expiry_bounds_are_validated(client):
    artifact = publish(client, b"<p>x</p>", "html")
    for seconds in (0, -5, 366 * 86_400):
        response = client.post(f"/internal/v1/artifacts/{artifact['id']}/shares",
                               json={"expires_in_seconds": seconds}, headers=AUTH)
        assert response.status_code == 422, seconds
    missing = client.post("/internal/v1/artifacts/7d3c0b55-1111-4a4a-9a9a-000000000000/shares", json={},
                          headers=AUTH)
    assert missing.status_code == 404


def test_render_policies_per_kind(client):
    md = publish(client, b"# Title\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))", "markdown")
    rendered = client.get(f"/internal/v1/artifacts/{md['id']}/render", headers=AUTH).text
    assert "<h1>Title</h1>" in rendered
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert 'href="javascript:' not in rendered
    svg = publish(client, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "svg")
    svg_doc = client.get(f"/internal/v1/artifacts/{svg['id']}/render", headers=AUTH).text
    assert '<img src="data:image/svg+xml;base64,' in svg_doc
    webp = publish(client, WEBP, "webp")
    webp_doc = client.get(f"/internal/v1/artifacts/{webp['id']}/render", params={"theme": "dark"}, headers=AUTH)
    assert '<img src="data:image/webp;base64,' in webp_doc.text and "color-scheme:dark" in webp_doc.text
    html = publish(client, b"<html><head></head><body>hi</body></html>", "html")
    html_doc = client.get(f"/internal/v1/artifacts/{html['id']}/render", headers=AUTH).text
    assert html_doc.index('http-equiv="Content-Security-Policy"') < html_doc.index("<html>")


def test_public_rate_limit_is_bounded(settings, clock):
    limited = TestClient(create_app(replace(settings, public_requests_per_minute=3), clock))
    codes = [limited.get(f"/{'A' * 22}").status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]


def test_access_log_never_contains_capability_paths(client, caplog):
    artifact = publish(client, b"<p>x</p>", "html")
    token = token_of(share(client, artifact["id"])["url"])
    with caplog.at_level(logging.INFO, logger="hermes_artifacts"):
        client.get(f"/{token}")
        client.get(f"/{token}/content")
        client.get("/someone-guessing")
    joined = "\n".join(caplog.messages)
    assert token not in joined and "someone-guessing" not in joined
    assert "GET /<share> 200" in joined


def test_backup_restores_artifacts_and_shares(client, settings, clock, tmp_path):
    artifact = publish(client, png_bytes(), "png", title="Pixel")
    token = token_of(share(client, artifact["id"])["url"])
    backup = tmp_path / "backup.db"
    Store(settings.database, clock).backup(backup)
    restored = TestClient(create_app(replace(settings, database=backup), clock))
    assert restored.get(f"/{token}/download").content == png_bytes()
    listed = restored.get("/internal/v1/artifacts", headers=AUTH).json()["items"]
    assert [a["title"] for a in listed] == ["Pixel"]


def test_content_only_renders_inside_a_frame(client):
    artifact = publish(client, b"<p>x</p>", "html")
    token = token_of(share(client, artifact["id"])["url"])
    for headers in ({}, {"Sec-Fetch-Dest": "document"}):
        top_level = client.get(f"/{token}/content", headers=headers, follow_redirects=False)
        assert top_level.status_code == 303
        assert top_level.headers["location"] == f"/{token}"
    assert client.get(f"/{token}/content", headers=FRAME).status_code == 200


def test_management_guard_runs_before_body_parsing(client):
    artifact = publish(client, b"<p>x</p>", "html")
    anonymous = client.post(f"/internal/v1/artifacts/{artifact['id']}/shares", content=b"{not json",
                            headers={"Content-Type": "application/json"})
    assert anonymous.status_code == 401
    oversized = client.post(f"/internal/v1/artifacts/{artifact['id']}/shares", content=b"{}",
                            headers={**AUTH, "Content-Type": "application/json", "Content-Length": str(10**9)})
    assert oversized.status_code == 413


def test_render_exposes_the_frame_policy_for_wrappers(client):
    artifact = publish(client, b"<p>x</p>", "html")
    rendered = client.get(f"/internal/v1/artifacts/{artifact['id']}/render", headers=AUTH)
    assert rendered.headers["x-artifact-frame-policy"].startswith("default-src 'none'")
    assert "frame-src 'none'" in rendered.headers["x-artifact-frame-policy"]


def test_rate_limit_groups_ipv6_clients_by_prefix():
    from app.main import client_key

    assert client_key("2001:db8::1") == client_key("2001:db8::ffff") == "2001:db8::/64"
    assert client_key("2001:db8:0:1::1") != client_key("2001:db8::1")
    assert client_key("::ffff:192.0.2.7") == client_key("192.0.2.7") == "192.0.2.7"


def test_slugs_are_stable_unique_and_identify_without_granting(client, settings, clock):
    first = publish(client, b"<p>1</p>", "html", title="Orbit Lab")
    second = publish(client, b"<p>2</p>", "html", title="Orbit  Lab!")
    third = publish(client, b"<p>3</p>", "html", title="Ørbit Lab")
    odd = publish(client, b"<p>4</p>", "html", title="***")
    assert [first["slug"], second["slug"], third["slug"], odd["slug"]] == ["orbit-lab", "orbit-lab-2", "rbit-lab", "artifact"]
    resolved = client.get("/internal/v1/slugs/orbit-lab-2", headers=AUTH)
    assert resolved.status_code == 200 and resolved.json()["artifact"]["id"] == second["id"]
    assert resolved.json()["shares"] == []
    for unknown in ("missing", "Orbit-Lab", "orbit--lab", "a" * 300):
        assert client.get(f"/internal/v1/slugs/{unknown}", headers=AUTH).status_code == 404
    assert client.get("/internal/v1/slugs/orbit-lab").status_code == 401
    assert client.get("/orbit-lab").status_code == 404
    restarted = TestClient(create_app(settings, clock))
    assert restarted.get("/internal/v1/slugs/orbit-lab", headers=AUTH).json()["artifact"]["id"] == first["id"]
    assert publish(restarted, b"<p>5</p>", "html", title="Orbit Lab")["slug"] == "orbit-lab-3"


def test_pre_slug_database_is_refused_not_migrated(tmp_path, clock):
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="no migration"):
        Store(path, clock)


def test_library_and_backup_files_are_owner_only_whatever_the_umask(tmp_path, clock):
    previous = os.umask(0o002)
    try:
        store = Store(tmp_path / "lib" / "a.db", clock)
        store.publish(title="Private", kind=Kind.MARKDOWN, content=b"# private", source_profile="p",
                      source_session_id=None)
        backup = tmp_path / "backups" / "b.db"
        store.backup(backup)
    finally:
        os.umask(previous)
    for path in (tmp_path / "lib", tmp_path / "lib" / "a.db", backup):
        assert path.stat().st_mode & 0o077 == 0, f"{path.name} is {oct(path.stat().st_mode & 0o777)}"
    with pytest.raises(FileExistsError):
        store.backup(backup)
