"""Guest-only bridge from the public edge to a host-loopback artifact service.

Traefik runs in Docker and cannot reach the service's 127.0.0.1 listener. This bridge listens on
the Docker host gateway and forwards ONLY the public viewer surface:

    GET/HEAD  /<22-char token>  /<token>/content  /<token>/download  /_viewer/viewer.(css|js)

Everything else (management, docs, encoded or odd paths) is answered by the service's own
"link unavailable" page, so a malformed link looks exactly like an unknown one. Other methods
get 405. Only an allowlist of request headers goes upstream (never Authorization, Cookie or
forwarded headers). The client address is taken from Traefik's X-Forwarded-For only when the
connection comes from a trusted proxy network, and from Cloudflare's CF-Connecting-IP only when
Traefik's peer is a Cloudflare edge. There is no access log: every path is a capability URL.

Stdlib only. Env: BRIDGE_LISTEN (host:port), BRIDGE_UPSTREAM (http://127.0.0.1:port),
BRIDGE_TRUSTED_PROXIES (comma-separated CIDRs of the reverse proxy's network).
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

GUEST_PATH = re.compile(r"/(?:[A-Za-z0-9_-]{22}(?:/content|/download)?|_viewer/viewer\.(?:css|js))?")
QUERY = re.compile(r"(?:theme=(?:light|dark))?")
FORWARD_HEADERS = ("accept", "accept-language", "if-none-match", "sec-fetch-dest", "sec-fetch-mode",
                   "sec-fetch-site", "user-agent")
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
              "proxy-authenticate", "proxy-authorization", "set-cookie", "server", "date"}
# https://www.cloudflare.com/ips/ — CF-Connecting-IP is believed only from these peers.
CLOUDFLARE = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
))
MAX_HEADER_VALUE = 1024


def ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    return address.ipv4_mapped or address if address.version == 6 else address


def client_address(peer: str, xff: str | None, cf_connecting_ip: str | None, trusted: tuple) -> str:
    """The guest's address, believing each hop only as far as it is trusted."""
    peer_ip = ip(peer)
    if peer_ip is None or not any(peer_ip in net for net in trusted):
        return str(peer_ip or peer)
    # Traefik trusts no forwarded headers, so it replaces X-Forwarded-For with its own peer.
    edge = ip((xff or "").split(",")[-1])
    if edge is None:
        return str(peer_ip)
    if any(edge in net for net in CLOUDFLARE):
        guest = ip(cf_connecting_ip)
        if guest is not None:
            return str(guest)
    return str(edge)


def upstream_target(method: str, raw_path: str) -> str | None:
    """Upstream path for an allowed guest request; None means 'answer as unavailable'."""
    if method not in ("GET", "HEAD") or "%" in raw_path or "\\" in raw_path:
        return None
    parts = urlsplit(raw_path)
    if parts.scheme or parts.netloc or not GUEST_PATH.fullmatch(parts.path):
        return None
    if parts.query and not (parts.path.endswith("/content") and QUERY.fullmatch(parts.query)):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


class Bridge(BaseHTTPRequestHandler):
    server_version = "artifacts-guest"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10  # an idle or slow guest connection must not hold a thread forever
    upstream: tuple[str, int]
    trusted: tuple

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - no access log: paths are secrets
        return

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        return

    def _forward(self) -> None:
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True  # never read a guest body; do not reuse the connection after one
        if self.command not in ("GET", "HEAD"):
            self._plain(405, "Method not allowed", {"Allow": "GET, HEAD"})
            return
        target = upstream_target(self.command, self.path) or "/"
        headers = {k: v for k in FORWARD_HEADERS if (v := self.headers.get(k)) and len(v) <= MAX_HEADER_VALUE}
        headers["X-Forwarded-For"] = client_address(self.client_address[0], self.headers.get("X-Forwarded-For"),
                                                    self.headers.get("CF-Connecting-IP"), self.trusted)
        headers["Host"] = "%s:%d" % self.upstream
        conn = http.client.HTTPConnection(*self.upstream, timeout=30)
        try:
            conn.request(self.command, target, headers=headers)
            reply = conn.getresponse()
            body = reply.read()
        except (OSError, http.client.HTTPException):
            self._plain(502, "Artifact service unavailable", {"Cache-Control": "no-store"})
            return
        finally:
            conn.close()
        self.send_response(reply.status)
        for name, value in reply.getheaders():
            if name.lower() not in HOP_BY_HOP and (name.lower() != "content-length" or self.command == "HEAD"):
                self.send_header(name, value)
        if self.command != "HEAD":
            self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            # The socket timeout bounds each blocking call, so write in slices: a slow but steady guest
            # gets the whole artifact, a stalled one is cut off after the timeout.
            view = memoryview(body)
            for start in range(0, len(view), 64 * 1024):
                self.wfile.write(view[start:start + 64 * 1024])

    def _plain(self, status: int, text: str, extra: dict) -> None:
        body = text.encode()
        self.send_response(status)
        if self.close_connection:
            extra = {**extra, "Connection": "close"}
        for name, value in {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body)),
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **extra}.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if code == 501:  # a method without a do_* handler (TRACE, CONNECT, ...)
            self.close_connection = True
            self._plain(405, "Method not allowed", {"Allow": "GET, HEAD"})
            return
        self._plain(code, "Bad request", {"Connection": "close"})
        self.close_connection = True


def make_server(listen: str, upstream: str, trusted: str) -> ThreadingHTTPServer:
    host, port = listen.rsplit(":", 1)
    up = urlsplit(upstream)
    if up.scheme != "http" or ip(up.hostname) is None or not ip(up.hostname).is_loopback:
        raise SystemExit("BRIDGE_UPSTREAM must be http://<loopback>:<port>")
    handler = type("ConfiguredBridge", (Bridge,), {
        "upstream": (up.hostname, up.port or 80),
        "trusted": tuple(ipaddress.ip_network(n.strip()) for n in trusted.split(",") if n.strip()),
    })
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = make_server(os.environ["BRIDGE_LISTEN"], os.environ["BRIDGE_UPSTREAM"],
                         os.environ.get("BRIDGE_TRUSTED_PROXIES", ""))
    print(f"guest bridge on {os.environ['BRIDGE_LISTEN']}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
