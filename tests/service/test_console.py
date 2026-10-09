"""Standalone console: configuration fails closed, Basic login guards page and API, writes need the origin."""

import base64
import logging
import secrets

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.console import BasicAuth, parse_basic
from app.main import create_app

PASSWORD = "synthetic-" + secrets.token_urlsafe(12)
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(10)).decode()
ORIGIN = "http://127.0.0.1:8080"
HOST = {"Host": "127.0.0.1:8080"}
TOKEN = "t" * 40
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def basic(user: str = "operator", password: str = PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


GOOD = {**HOST, "Authorization": basic(), "X-Artifacts-Console": "1"}
WRITE = {**GOOD, "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}


def env(**extra) -> dict:
    return {"ARTIFACTS_API_TOKEN": TOKEN, "ARTIFACTS_DB": "x.db", **extra}


def console_env(**extra) -> dict:
    return env(ARTIFACTS_CONSOLE_ORIGIN=ORIGIN, ARTIFACTS_CONSOLE_USER="operator",
               ARTIFACTS_CONSOLE_PASSWORD_HASH=HASH, **extra)


@pytest.fixture
def client(tmp_path):
    settings = Settings.from_env({**console_env(), "ARTIFACTS_DB": str(tmp_path / "a.db"),
                                  "ARTIFACTS_PUBLIC_URL": ORIGIN})
    with TestClient(create_app(settings), base_url=ORIGIN) as c:
        yield c


def publish(client, title="Quarterly report", profile="prime") -> dict:
    r = client.post("/internal/v1/artifacts", params={"title": title, "kind": "markdown", "source_profile": profile},
                    content=b"# Hello", headers=BEARER)
    assert r.status_code == 201
    return r.json()


def test_embed_only_is_the_default_and_console_is_unavailable(tmp_path):
    settings = Settings.from_env(env(ARTIFACTS_DB=str(tmp_path / "a.db")))
    assert settings.console is None
    with TestClient(create_app(settings), base_url=ORIGIN) as c:
        for path in ("/console", "/console/", "/console/api/artifacts", "/console/assets/console.js"):
            r = c.get(path, headers=GOOD)
            assert r.status_code == 404 and "unavailable" in r.text


@pytest.mark.parametrize("extra, message", [
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": None}, "needs ARTIFACTS_CONSOLE_USER"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": "plaintext-password"}, "not a bcrypt hash"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": "$apr1$abc$0123456789012345678901"}, "not a bcrypt hash"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": HASH[:-1]}, "not a bcrypt hash"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": "$2y$05$" + HASH[7:]}, "cost 5"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": "$2y$20$" + HASH[7:]}, "cost 20"),
    ({"ARTIFACTS_CONSOLE_PASSWORD_HASH": HASH + "é"}, "not a bcrypt hash"),
    ({"ARTIFACTS_CONSOLE_PASSWORD": "hunter2hunter2"}, "is refused"),
    ({"ARTIFACTS_CONSOLE_ORIGIN": "http://artifacts.example.com"}, "must use https"),
    ({"ARTIFACTS_CONSOLE_ORIGIN": "https://artifacts.example.com/console"}, "bare origin"),
    ({"ARTIFACTS_CONSOLE_ORIGIN": "https://u:p@artifacts.example.com"}, "bare origin"),
    ({"ARTIFACTS_CONSOLE_ORIGIN": "javascript:alert(1)"}, r"an http\(s\) origin"),
    ({"ARTIFACTS_CONSOLE_ORIGIN": None}, "ARTIFACTS_CONSOLE_ORIGIN is not"),
    ({"ARTIFACTS_CONSOLE_USER": "op:erator"}, "without ':'"),
    ({"ARTIFACTS_CONSOLE_USER": "x" * 65}, "1-64"),
])
def test_bad_console_configuration_fails_closed(extra, message):
    values = console_env()
    for key, value in extra.items():
        if value is None:
            values.pop(key)
        else:
            values[key] = value
    with pytest.raises(ConfigError, match=message) as exc:
        Settings.from_env(values)
    assert HASH not in str(exc.value) and PASSWORD not in str(exc.value)


def test_htpasswd_file_is_accepted_and_validated(tmp_path):
    good = tmp_path / "htpasswd"
    good.write_text(f"# operator login\noperator:{HASH.replace('$2b$', '$2y$')}\n")
    console = Settings.from_env(env(ARTIFACTS_CONSOLE_ORIGIN="https://Artifacts.Example.com:443",
                                    ARTIFACTS_CONSOLE_HTPASSWD_FILE=str(good))).console
    assert console.origin == "https://artifacts.example.com" and console.username == "operator"
    assert BasicAuth(console).verify(basic()) and HASH not in repr(console)
    two = tmp_path / "two"
    two.write_text(f"a:{HASH}\nb:{HASH}\n")
    with pytest.raises(ConfigError, match="exactly one"):
        Settings.from_env(env(ARTIFACTS_CONSOLE_ORIGIN=ORIGIN, ARTIFACTS_CONSOLE_HTPASSWD_FILE=str(two)))
    with pytest.raises(ConfigError, match="cannot be read"):
        Settings.from_env(env(ARTIFACTS_CONSOLE_ORIGIN=ORIGIN, ARTIFACTS_CONSOLE_HTPASSWD_FILE=str(tmp_path / "no")))


@pytest.mark.parametrize("header", [
    "", "Bearer abc", "Basic", "Basic !!!!", "Basic " + base64.b64encode(b"no-colon").decode(),
    "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(), "Basic " + base64.b64encode(b":" + b"p" * 20).decode(),
    "Basic " + base64.b64encode(b"operator:").decode(), "Basic " + "A" * 600,
    "Basic " + base64.b64encode(("operator:" + "é" * 40).encode()).decode(),
])
def test_malformed_basic_headers_are_no_credential(header):
    assert parse_basic(header) is None


def test_long_and_wrong_passwords_are_rejected(client):
    for header in (basic(password=PASSWORD + "x"), basic(user="Operator"), basic(password="p" * 73),
                   basic(password="пароль-" * 6)):
        assert client.get("/console/", headers={**HOST, "Authorization": header}).status_code == 401


def test_console_page_and_assets_require_the_login(client):
    for path in ("/console", "/console/", "/console/quarterly-report", "/console/assets/console.js"):
        r = client.get(path, headers=HOST)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic realm=")
        assert r.headers["cache-control"] == "no-store"
    r = client.get("/console/", headers={**HOST, "Authorization": basic()})
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/console/assets/other.js", headers={**HOST, "Authorization": basic()}).status_code == 404


def test_proxy_identity_headers_never_authenticate(client):
    for name in ("X-Forwarded-User", "X-Remote-User", "Remote-User", "X-Auth-Request-User", "X-Forwarded-Email"):
        r = client.get("/console/api/artifacts", headers={**HOST, "X-Artifacts-Console": "1", name: "operator"})
        assert r.status_code == 401
    assert client.get("/console/api/artifacts", headers={**HOST, **BEARER, "X-Artifacts-Console": "1"}).status_code == 401


def test_private_api_and_downloads_need_credentials(client):
    artifact = publish(client)
    paths = ["/console/api/artifacts", "/console/api/profiles", f"/console/api/artifacts/{artifact['id']}",
             f"/console/api/slugs/{artifact['slug']}", f"/console/api/artifacts/{artifact['id']}/download"]
    for path in paths:
        assert client.get(path, headers={**HOST, "X-Artifacts-Console": "1"}).status_code == 401
        assert client.get(path, headers=GOOD).status_code == 200
    r = client.get(f"/console/api/artifacts/{artifact['id']}/download", headers=GOOD)
    assert r.content == b"# Hello" and r.headers["content-disposition"].startswith("attachment")


def test_console_only_answers_on_its_own_host(client):
    r = client.get("/console/api/artifacts", headers={**GOOD, "Host": "art.example.com"})
    assert r.status_code == 404 and "unavailable" in r.text


def test_api_calls_need_the_console_header_and_writes_need_the_origin(client):
    artifact = publish(client)
    shares = f"/console/api/artifacts/{artifact['id']}/shares"
    no_header = {k: v for k, v in GOOD.items() if k != "X-Artifacts-Console"}
    assert client.get("/console/api/artifacts", headers=no_header).status_code == 403
    assert client.get("/console/api/artifacts", headers={**GOOD, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    body = {"expires_in_seconds": 3600}
    assert client.post(shares, json=body, headers=GOOD).status_code == 403
    assert client.post(shares, json=body, headers={**WRITE, "Origin": "https://evil.example"}).status_code == 403
    assert client.post(shares, json=body, headers={**WRITE, "Origin": "null"}).status_code == 403
    assert client.post(shares, json={}, headers=WRITE).status_code == 422
    assert client.post(shares, json={"expires_in_seconds": True}, headers=WRITE).status_code == 422
    assert client.post(shares, content=b"x" * 20_000, headers=WRITE).status_code == 413
    created = client.post(shares, json=body, headers=WRITE)
    assert created.status_code == 201
    url = created.json()["url"]
    assert url.startswith(ORIGIN + "/") and len(url.rsplit("/", 1)[1]) == 22
    assert client.get(url.removeprefix(ORIGIN)).status_code == 200
    share_id = created.json()["share"]["id"]
    assert client.delete(f"/console/api/shares/{share_id}", headers=GOOD).status_code == 403
    assert client.delete(f"/console/api/shares/{share_id}", headers=WRITE).json()["share"]["status"] == "revoked"
    assert client.get(url.removeprefix(ORIGIN)).status_code == 404


def test_preview_frame_is_sandboxed_and_mints_no_share(client):
    artifact = publish(client)
    frame = f"/console/api/artifacts/{artifact['id']}/frame?theme=dark"
    nav = {**HOST, "Authorization": basic(), "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"}
    assert client.get(frame, headers={**nav, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get(frame, headers={**nav, "Sec-Fetch-Dest": "document"}).status_code == 403
    r = client.get(frame, headers=nav)
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp and "frame-src 'none'" in csp and "connect-src 'none'" in csp
    assert 'sandbox="allow-scripts"' in r.text and "allow-same-origin" not in r.text
    detail = client.get(f"/console/api/artifacts/{artifact['id']}", headers=GOOD).json()
    assert detail["shares"] == [] and detail["artifact"]["active_shares"] == 0


def test_public_root_and_guest_paths_never_list_private_artifacts(client):
    publish(client, "Secret plans")
    for path in ("/", "/console/api", "/internal/v1/artifacts"):
        r = client.get(path, headers=HOST)
        assert "Secret plans" not in r.text and r.status_code in (401, 404)


def test_attribution_is_a_filter_not_an_owner(client):
    publish(client, "From prime", "prime")
    publish(client, "From steve", "steve")
    titles = {a["title"] for a in client.get("/console/api/artifacts", headers=GOOD).json()["items"]}
    assert titles == {"From prime", "From steve"}
    only = client.get("/console/api/artifacts?profile=steve", headers=GOOD).json()["items"]
    assert [a["title"] for a in only] == ["From steve"]


def test_failed_logins_are_rate_limited(client):
    statuses = [client.get("/console/", headers={**HOST, "Authorization": basic(password=f"wrong-{i}")}).status_code
                for i in range(25)]
    assert statuses[:20] == [401] * 20 and statuses[-1] == 429


def test_credentials_never_reach_the_log(client, caplog):
    caplog.set_level(logging.INFO, logger="hermes_artifacts")
    client.get("/console/", headers={**HOST, "Authorization": basic()})
    client.get("/console/", headers={**HOST, "Authorization": basic(password="wrong-password")})
    text = caplog.text
    assert "GET /console/ 200" in text and PASSWORD not in text and basic().split()[1] not in text


def test_frame_exemption_is_only_the_preview_route(client):
    nav = {**HOST, "Authorization": basic(), "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"}
    assert client.get("/console/api/slugs/frame", headers=nav).status_code == 403
    assert client.delete("/console/api/shares/frame", headers=nav).status_code == 403


def test_login_attempts_share_a_global_budget():
    console = Settings.from_env(console_env()).console
    auth = BasicAuth(console, clock=lambda: 1000.0, all_attempts_per_minute=120)
    allowed = [auth.allow_attempt(f"10.0.0.{i}") and auth.allow_globally() for i in range(200)]
    assert all(allowed[:120]) and not any(allowed[120:])


def test_console_api_bodies_need_a_declared_length_and_answers_are_not_cached(client):
    artifact = publish(client)
    response = client.post(f"/console/api/artifacts/{artifact['id']}/shares", headers=WRITE,
                           content=iter([b'{"expires_in_seconds": ', b"600}"]))
    assert response.status_code == 411
    listing = client.get("/console/api/artifacts", headers=GOOD)
    assert listing.headers["cache-control"] == "no-store"
    assert listing.headers["x-content-type-options"] == "nosniff"
