"""deploy/guest_bridge.py in front of the real service: only the guest surface gets through."""

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from deploy.guest_bridge import client_address, make_server, upstream_target

TOKEN = "t" * 40
CLOUDFLARE_EDGE = "172.68.1.2"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port: int, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    reply = conn.getresponse()
    data = reply.read()
    conn.close()
    return reply.status, data, {k.lower(): v for k, v in reply.getheaders()}


def serve_bridge(upstream_port: int, trusted: str = "127.0.0.0/8"):
    port = free_port()
    server = make_server(f"127.0.0.1:{port}", f"http://127.0.0.1:{upstream_port}", trusted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    settings = Settings(database=tmp_path_factory.mktemp("bridge") / "a.db", api_token=TOKEN,
                        public_url="https://art.example.test")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error",
                                           proxy_headers=True, forwarded_allow_ips="127.0.0.1"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    bridge, bridge_port = serve_bridge(port)
    yield port, bridge_port
    bridge.shutdown()
    server.should_exit = True


def manage(port: int, method: str, path: str, body: bytes | None = None, content_type: str = "application/json"):
    status, data, _ = request(port, method, f"/internal/v1{path}",
                              {"Authorization": f"Bearer {TOKEN}", "Content-Type": content_type}, body)
    return status, json.loads(data) if data else None


@pytest.fixture(scope="module")
def grant(service):
    port, _ = service
    _, artifact = manage(port, "POST", "/artifacts?title=Bridge%20probe&kind=html&source_profile=prime",
                         b"<!doctype html><p>bridge probe</p>", "text/html")
    _, share = manage(port, "POST", f"/artifacts/{artifact['id']}/shares", b'{"expires_in_seconds": 3600}')
    return artifact, share, share["url"].rsplit("/", 1)[1]


def test_active_token_opens_shell_content_and_download(service, grant):
    _, bridge = service
    _, _, token = grant
    for path, headers in ((f"/{token}", {}), (f"/{token}/content?theme=dark", {"Sec-Fetch-Dest": "iframe"}),
                          (f"/{token}/download", {})):
        got, body, h = request(bridge, "GET", path, headers)
        head, head_body, hh = request(bridge, "HEAD", path, headers)
        assert (got, head) == (200, 200), path
        assert head_body == b"" and hh["content-length"] == str(len(body)) == h["content-length"]
        assert "set-cookie" not in h
    status, _, h = request(bridge, "GET", "/_viewer/viewer.js")
    assert status == 200 and h["content-type"].startswith("text/javascript")


def test_top_level_content_still_redirects_to_the_shell(service, grant):
    _, bridge = service
    status, _, h = request(bridge, "GET", f"/{grant[2]}/content")
    assert status == 303 and h["location"] == f"/{grant[2]}"


def test_everything_else_is_the_unavailable_page(service, grant):
    port, bridge = service
    artifact, _, token = grant
    unknown = request(bridge, "GET", "/" + "A" * 22)
    assert unknown[0] == 404
    creds = {"Authorization": f"Bearer {TOKEN}", "Cookie": "hermes_session=x"}
    for path in ["/internal/v1/artifacts", f"/internal/v1/artifacts/{artifact['id']}/download", "/healthz",
                 "/docs", "/openapi.json", "/redoc", "/debug", "/metrics", "//internal/v1/artifacts",
                 "/%2e%2e/internal/v1/artifacts", "/internal%2Fv1%2Fartifacts", f"/{token}%2F..%2Finternal",
                 f"/{token}/../internal/v1/artifacts", f"/{token}/download?cursor=x", f"/{token}x", token[:21],
                 "/_viewer/viewer.html", "http://127.0.0.1/internal/v1/artifacts", "/" + "A" * 21 + "!"]:
        for method in ("GET", "HEAD"):
            status, body, h = request(bridge, method, path if path.startswith(("/", "http")) else "/" + path, creds)
            assert status == 404, (method, path, status)
            assert body == (unknown[1] if method == "GET" else b""), path
            assert "application/json" not in h.get("content-type", "")


def test_write_and_other_methods_never_reach_the_service(service, grant):
    port, bridge = service
    artifact, _, token = grant
    before = manage(port, "GET", f"/artifacts/{artifact['id']}")[1]["shares"]
    creds = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    for method, path in [("POST", f"/internal/v1/artifacts/{artifact['id']}/shares"),
                         ("DELETE", f"/internal/v1/shares/{grant[1]['share']['id']}"),
                         ("PUT", f"/{token}"), ("PATCH", f"/{token}"), ("OPTIONS", f"/{token}"),
                         ("TRACE", f"/{token}"), ("POST", f"/{token}/download")]:
        status, _, h = request(bridge, method, path, creds, b"{}")
        assert status == 405 and h["allow"] == "GET, HEAD", (method, path)
    assert manage(port, "GET", f"/artifacts/{artifact['id']}")[1]["shares"] == before
    assert request(bridge, "GET", f"/{token}")[0] == 200


def test_revoked_token_is_the_unavailable_page(service, grant):
    port, bridge = service
    artifact = grant[0]
    _, share = manage(port, "POST", f"/artifacts/{artifact['id']}/shares", b"{}")
    token = share["url"].rsplit("/", 1)[1]
    assert request(bridge, "GET", f"/{token}")[0] == 200
    manage(port, "DELETE", f"/shares/{share['share']['id']}")
    for path in (f"/{token}", f"/{token}/content", f"/{token}/download"):
        assert request(bridge, "GET", path, {"Sec-Fetch-Dest": "iframe"})[:2] == request(bridge, "GET", "/" + "B" * 22)[:2]


class Capture(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        Capture.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(200)
        self.send_header("Set-Cookie", "leak=1")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_only_allowlisted_headers_and_a_derived_client_address_go_upstream():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Capture)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    bridge, port = serve_bridge(upstream_port)
    try:
        token = "C" * 22
        status, _, h = request(port, "GET", f"/{token}", {
            "Authorization": "Bearer secret", "Cookie": "hermes_session=x", "Forwarded": "for=6.6.6.6",
            "X-Forwarded-For": f"6.6.6.6, {CLOUDFLARE_EDGE}", "CF-Connecting-IP": "203.0.113.9",
            "X-Real-IP": "6.6.6.6", "X-Forwarded-Host": "evil.test", "Sec-Fetch-Dest": "document",
            "Accept": "text/html"})
        assert status == 200 and "set-cookie" not in h
        path, headers = Capture.seen[-1]
        assert path == f"/{token}"
        assert set(headers) <= {"accept", "sec-fetch-dest", "x-forwarded-for", "host", "accept-encoding"}
        assert headers["x-forwarded-for"] == "203.0.113.9"
    finally:
        bridge.shutdown()
        upstream.shutdown()


@pytest.mark.parametrize(("peer", "xff", "cf", "expected"), [
    ("198.51.100.7", "6.6.6.6", "6.6.6.7", "198.51.100.7"),          # untrusted peer: headers ignored
    ("172.18.0.10", "198.51.100.8", "6.6.6.6", "198.51.100.8"),      # direct client: CF header ignored
    ("172.18.0.10", "6.6.6.6, 198.51.100.8", None, "198.51.100.8"),  # only Traefik's own hop counts
    ("172.18.0.10", CLOUDFLARE_EDGE, "203.0.113.9", "203.0.113.9"),  # Cloudflare edge vouches for the guest
    ("172.18.0.10", "2a06:98c0::1", "2001:db8::5", "2001:db8::5"),
    ("172.18.0.10", CLOUDFLARE_EDGE, "not-an-ip", CLOUDFLARE_EDGE),
    ("172.18.0.10", None, None, "172.18.0.10"),
])
def test_client_address_trusts_each_hop_only_as_far_as_configured(peer, xff, cf, expected):
    import ipaddress
    assert client_address(peer, xff, cf, (ipaddress.ip_network("172.18.0.0/16"),)) == expected


def test_upstream_target_shapes():
    token = "D" * 22
    assert upstream_target("GET", f"/{token}") == f"/{token}"
    assert upstream_target("HEAD", f"/{token}/content?theme=light") == f"/{token}/content?theme=light"
    assert upstream_target("GET", f"/{token}/content?theme=red") is None
    assert upstream_target("POST", f"/{token}") is None
    assert upstream_target("GET", "/") == "/"


def test_bridge_refuses_a_non_loopback_upstream():
    with pytest.raises(SystemExit):
        make_server("127.0.0.1:0", "http://172.17.0.1:8080", "")
    assert Path(__file__).exists()


def test_idle_guest_connections_are_closed(service):
    _, bridge = service
    with socket.create_connection(("127.0.0.1", bridge), timeout=30) as idle:
        started = time.monotonic()
        assert idle.recv(1) == b""
    assert time.monotonic() - started < 25


def test_a_steady_slow_reader_gets_the_whole_large_download(service):
    port, _ = service
    body = b"<!doctype html><p>" + b"x" * (9 * 1024 * 1024)
    _, artifact = manage(port, "POST", "/artifacts?title=Large&kind=html&source_profile=prime", body, "text/html")
    _, share = manage(port, "POST", f"/artifacts/{artifact['id']}/shares", b'{"expires_in_seconds": 600}')
    bridge, bridge_port = serve_bridge(port)
    bridge.RequestHandlerClass.timeout = 0.5
    try:
        with socket.socket() as guest:
            guest.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
            guest.connect(("127.0.0.1", bridge_port))
            guest.sendall(f"GET /{share['url'].rsplit('/', 1)[1]}/download HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            received = b""
            while chunk := guest.recv(512 * 1024):
                received += chunk
                time.sleep(0.15)
        assert received.endswith(body)
    finally:
        bridge.shutdown()
