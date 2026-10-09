"""Bounded, authenticated client for the artifact service (stdlib only, Python 3.11+).

The service credential stays in the Hermes process environment. It is never
returned to callers, logged, or placed in a URL.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

URL_ENV = "HERMES_ARTIFACTS_URL"
TOKEN_ENV = "HERMES_ARTIFACTS_TOKEN"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def to_json(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class ArtifactService:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> Optional["ArtifactService"]:
        url, token = os.environ.get(URL_ENV, "").strip(), os.environ.get(TOKEN_ENV, "").strip()
        return cls(url, token) if url and token else None

    def __repr__(self) -> str:
        return f"ArtifactService({self.base_url!r})"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[bytes] = None,
        json_body: Any = None,
        content_type: str = "application/octet-stream",
    ) -> Reply:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.base_url}/internal/v1{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if body is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                return Reply(response.status, dict(response.headers), _read_capped(response))
        except urllib.error.HTTPError as exc:
            raw = _read_capped(exc)
            try:
                error = json.loads(raw.decode("utf-8"))["error"]
                raise ServiceError(exc.code, error["code"], error["message"]) from None
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                raise ServiceError(exc.code, "service_error", f"artifact service returned HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServiceError(502, "service_unreachable", f"artifact service is unreachable: {reason}") from None

    def publish(self, *, title: str, kind: str, content: bytes, source_profile: str,
                source_session_id: Optional[str]) -> dict:
        params = {"title": title, "kind": kind, "source_profile": source_profile,
                  "source_session_id": source_session_id}
        return self.request("POST", "/artifacts", params=params, body=content).json()

    def list(self, **filters: Any) -> dict:
        return self.request("GET", "/artifacts", params=filters).json()

    def profiles(self) -> dict:
        return self.request("GET", "/profiles").json()

    def get_by_slug(self, slug: str) -> dict:
        return self.request("GET", f"/slugs/{_segment(slug)}").json()

    def get(self, artifact_id: str) -> dict:
        return self.request("GET", f"/artifacts/{_segment(artifact_id)}").json()

    def render(self, artifact_id: str, theme: str) -> dict:
        """The sandboxed document plus the CSP its embedding wrapper must enforce."""
        reply = self.request("GET", f"/artifacts/{_segment(artifact_id)}/render", params={"theme": theme})
        policy = {k.lower(): v for k, v in reply.headers.items()}.get("x-artifact-frame-policy", "")
        if not policy:
            raise ServiceError(502, "service_error", "artifact service omitted the frame policy")
        return {"html": reply.body.decode("utf-8"), "policy": policy}

    def download(self, artifact_id: str) -> Reply:
        return self.request("GET", f"/artifacts/{_segment(artifact_id)}/download")

    def share(self, artifact_id: str, expires_in_seconds: Optional[int]) -> dict:
        body = {} if expires_in_seconds is None else {"expires_in_seconds": expires_in_seconds}
        return self.request("POST", f"/artifacts/{_segment(artifact_id)}/shares", json_body=body).json()

    def revoke(self, share_id: str) -> dict:
        return self.request("DELETE", f"/shares/{_segment(share_id)}").json()


def _segment(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _read_capped(response) -> bytes:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ServiceError(502, "response_too_large", "artifact service response exceeded the client limit")
    return data
