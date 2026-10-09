# Machine API

Agents and scripts outside Hermes manage the library through `/internal/v1`. Every request needs the machine credential as a bearer token. Never expose this API to the internet; route only the guest paths through your proxy ([deploy.md](deploy.md)).

```sh
BASE=http://127.0.0.1:8080
AUTH="Authorization: Bearer $(cat /path/to/api-token)"
```

Errors are JSON: `{"error": {"code": "<code>", "message": "..."}}` with codes such as `unauthorized` (401), `not_found` (404), `length_required` (411: send a `Content-Length`; only publish accepts a chunked body), `too_large` (413) and `invalid_artifact` or `invalid_request` (422).

## Publish

`POST /internal/v1/artifacts?title=&kind=&source_profile=&source_session_id=` with the raw file as the body. Returns 201 and the artifact. It creates no share.

| Parameter | Required | Notes |
|---|---|---|
| `title` | yes | Up to 200 characters after control characters are replaced. The slug is derived from it and never changes. |
| `kind` | yes | `html`, `markdown`, `svg`, `png` or `webp`. HTML must be self-contained. |
| `source_profile` | no | Attribution label and list filter, default `unknown`. Not an owner or a permission. |
| `source_session_id` | no | Provenance label: 1-128 characters from `A-Z a-z 0-9 _ . : -`. |

```sh
curl -fsS -X POST "$BASE/internal/v1/artifacts?title=Weekly%20report&kind=html&source_profile=research" \
  -H "$AUTH" -H 'Content-Type: text/html' --data-binary @report.html
```

```json
{"id": "6f1c…", "slug": "weekly-report", "title": "Weekly report", "kind": "html",
 "media_type": "text/html; charset=utf-8", "created_at": 1791400000, "source_profile": "research",
 "source_session_id": null, "content_sha256": "…", "byte_length": 5120, "active_shares": 0}
```

## Read

| Request | Returns |
|---|---|
| `GET /internal/v1/artifacts?q=&kind=&profile=&limit=&cursor=` | `{"items": [...], "next_cursor": ...}`, newest first, metadata only. `limit` 1-100 (default 30). Pass `next_cursor` back as `cursor`. |
| `GET /internal/v1/artifacts/{id}` | `{"artifact": {...}, "shares": [...]}` with each share's status. |
| `GET /internal/v1/slugs/{slug}` | Same as above, looked up by slug. |
| `GET /internal/v1/artifacts/{id}/download` | The original bytes as an attachment. |
| `GET /internal/v1/artifacts/{id}/render?theme=light` | The sandboxed render document (HTML) with its content policy. |
| `GET /internal/v1/profiles` | `{"profiles": [...]}`, every attribution label in use. |

```sh
curl -fsS "$BASE/internal/v1/artifacts?kind=html&limit=10" -H "$AUTH" | jq '.items[] | {id, title}'
curl -fsS "$BASE/internal/v1/artifacts/$ID/download" -H "$AUTH" -o copy.html
```

## Share

`POST /internal/v1/artifacts/{id}/shares` with an optional JSON body `{"expires_in_seconds": N}`. Without a body the default expiry applies (7 days); `N` must be between 1 and `ARTIFACTS_MAX_SHARE_SECONDS`.

```sh
curl -fsS -X POST "$BASE/internal/v1/artifacts/$ID/shares" \
  -H "$AUTH" -H 'Content-Type: application/json' -d '{"expires_in_seconds": 86400}'
```

```json
{"share": {"id": "0b9e…", "artifact_id": "6f1c…", "created_at": 1791400000,
           "expires_at": 1791486400, "revoked_at": null, "status": "active"},
 "url": "https://share.example.com/<22-character token>"}
```

The `url` appears only in this response; the service keeps only the token's SHA-256. `GET /internal/v1/artifacts/{id}/shares` lists shares without URLs.

## Revoke

`DELETE /internal/v1/shares/{share_id}` returns `{"share": {..., "status": "revoked"}}`. Other shares of the same artifact keep working.

```sh
curl -fsS -X DELETE "$BASE/internal/v1/shares/$SHARE_ID" -H "$AUTH"
```

## Guest surface

These need no credential; the token is the grant. Only `GET` and `HEAD` are served.

| Path | Serves |
|---|---|
| `/<token>` | The viewer page. |
| `/<token>/content` | The sandboxed artifact, only as an iframe destination (`Sec-Fetch-Dest: iframe`); opened directly it redirects to the viewer. |
| `/<token>/download` | The original bytes as an attachment. |
| `/healthz` | `{"status": "ok"}`. |

An expired, revoked, unknown or malformed token returns the same 404 page on every path.
