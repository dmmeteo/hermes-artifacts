# Security model

## Assets and trust

- **Trusted:** the service code, the plugin, the dashboard page, the guest shell and the dashboard's trusted wrapper frame.
- **Untrusted:** every artifact payload. Agents generate HTML, SVG and Markdown, and that content may be hostile (prompt injection, copied web content).
- **Secrets:** the service machine credential (server-side only: service env/secret file and the Hermes `.env`), the standalone console login (bcrypt hash in env or an htpasswd file; the password exists only in the operator's browser), share tokens (capability URLs), Hermes dashboard sessions.
- **Trusted hosts of the panel:** the service's `/console/` page and the Hermes dashboard tab. The panel itself is trusted code; it never holds a credential.

## Controls

| Threat | Control | Evidence |
|---|---|---|
| Anonymous management | Bearer credential with `hmac.compare_digest`, enforced by an ASGI guard before body parsing and again by the router; the guest-only bridge never forwards `/internal/` | `test_management_*`, UAT "service ... without credential is 401" |
| Anonymous dashboard API | Hermes session auth in front of `/api/plugins/artifacts/*`; user-plugin backends mount only when enabled | UAT "anonymous ... rejected by Hermes auth"; Hermes contract tests in verify |
| Credential exposure | Credential never in browser JS, URLs or responses; plugin maps service 401 to a generic 502; client follows no redirects | `test_dashboard_maps_service_auth_failure_away_from_401`; UAT secret scan |
| Capability token leakage | 128-bit tokens (22 base64url characters from `secrets.token_urlsafe(16)`), SHA-256 at rest, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `noindex`, access log writes `/<share>`, Traefik route logs off | `test_access_log_never_contains_capability_paths`; UAT log/evidence scan; container log check |
| Fail-open links | Single resolver checks format, existence, revocation and expiry on shell, content and download for GET and HEAD, returning one identical 404 | `test_expired_revoked_unknown_and_malformed_links_fail_closed`; UAT lifecycle |
| Artifact code in the dashboard origin | Artifacts never enter the Hermes DOM. They run in `sandbox="allow-scripts"` frames (opaque origin) without `allow-same-origin`, popups, forms, top navigation or downloads. `/document` returns JSON, `/download` is an attachment with a sandbox CSP | UAT probes: origin `null`; cookie, storage, parent and top access all blocked |
| Network exfiltration | Content CSP: `default-src 'none'`, inline script/style only, `connect-src 'none'`, data/blob media only, `form-action 'none'`, `base-uri 'none'`, `frame-src 'none'`. Markdown renders with raw HTML disabled and unsafe link schemes dropped. SVG renders inside `<img>` | UAT: API, management and external fetches blocked; external image blocked with reason `csp`; Markdown and SVG scripts inert |
| Frame self-navigation (phishing, exfiltration) | Dashboard: trusted wrapper frame whose CSP (`frame-src 'none'`) governs the artifact frame's navigations. Guest: shell CSP `frame-src 'self'`. Both restore the artifact and show a notice. `/content` serves only `Sec-Fetch-Dest: iframe`; a top-level open redirects to the shell | UAT: no response ever received from the external host; notice shown; `/content` top-level lands on the shell |
| Anonymous standalone console | Off unless `ARTIFACTS_CONSOLE_ORIGIN` is set. Then `ConsoleGuard` (ASGI, before routing) requires a Basic login checked with `bcrypt.checkpw` against the configured hash for the page, assets, API, preview frame and downloads. Misconfiguration (no login, no origin, plaintext or non-bcrypt or cost < 10 hash, `ARTIFACTS_CONSOLE_PASSWORD`, non-loopback `http` origin) stops startup | `test_console.py`; `verify_concept.py` misconfiguration and 401 checks |
| Proxy-asserted identity | `X-Forwarded-User`, `Remote-User` and similar are never read; the machine bearer token is refused on `/console/`; the console login is refused on `/internal/` | `test_proxy_identity_headers_never_authenticate`; concept checks |
| Cross-site requests riding the browser's Basic credentials (CSRF) | Every console API call needs `X-Artifacts-Console: 1` (not sendable cross-origin without a CORS preflight, which is never granted); writes also need `Origin` equal to the configured origin and `Sec-Fetch-Site: same-origin` when sent; the preview frame route needs `Sec-Fetch-Dest: iframe` and `Sec-Fetch-Site: same-origin`; the shell refuses framing (`frame-ancestors 'none'`, `X-Frame-Options: DENY`) | `test_api_calls_need_the_console_header_and_writes_need_the_origin`; concept CSRF checks |
| Password guessing | 20 bcrypt verifications per client per minute, then 429; verified headers cached 5 minutes under an HMAC with a per-process random key | `test_failed_logins_are_rate_limited` |
| Console on the guest host | The guard answers only when `Host` equals the configured origin's host; elsewhere `/console` is the unavailable page | `test_console_only_answers_on_its_own_host` |
| Credentials in the Hermes-hosted panel | The panel iframe receives no token. It asks the host tab, by `postMessage` restricted to the same origin and the host's own window, to fetch one of eight allowlisted API shapes with `authedFetch`; hostile slugs and unknown paths are ignored or answered 400 | `verify_concept.py`: message, storage and URL secret scans; hostile navigate and request checks |
| Resource exhaustion | 10 MiB per artifact (declared length rejected early, stream capped), other management and console bodies need a declared length (chunked bodies get 411), list pages ≤ 100, uvicorn concurrency limit, per-client token bucket with a bounded table, 32 MiB client response cap, guest-bridge connections time out after 10 s idle; container memory and pid limits | `test_publish_enforces_size_limit`, `test_public_rate_limit_is_bounded`, `test_bodies_without_a_declared_length_are_refused_except_the_streamed_publish`, `test_idle_guest_connections_are_closed` |
| Library at rest | A new database and every `app.backup` copy are created 0600 in a 0700 folder whatever the umask; backups open the source read-only | `test_library_and_backup_files_are_owner_only_whatever_the_umask`, `test_backup_command_copies_without_touching_the_source` |

## Read-only security review (2026-10-06)

A native read-only reviewer subagent read the implementation at pre-release development revision `8aab879` (not part of this repository's public history). It found no high-severity issues. Disposition:

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Dashboard preview frame could navigate itself to an external URL; reload detection fired only after the request left | Medium | **Fixed** with the trusted wrapper frame. UAT asserts that no response came from the external host |
| 2 | `/<token>/content` opened top-level becomes a phishing or redirect page on the share domain | Medium/low | **Fixed**: only an iframe destination renders it, otherwise 303 to the shell. The `<noscript>` direct link was removed |
| 3 | WebRTC and DNS prefetch are not governed by CSP; guest frame URL contains the token | Low | **Accepted residual.** CSP is not a network air gap. A hostile artifact could beacon its own content, the viewer's IP or its own link through WebRTC. Mitigation for sensitive content is not to share it |
| 4 | FastAPI read JSON bodies before the auth dependency (pre-auth body parse, route probing) | Low | **Fixed**: ASGI `InternalGuard` authenticates and checks declared length first |
| 5 | `?a=` could inject path segments into authenticated dashboard fetches | Low | **Fixed**: UUID validation and `encodeURIComponent`. The address is now `/artifacts/<slug>`: the page and adapter accept only `[a-z0-9-]` slugs and still encode the segment |
| 6 | Per-IP limit bypassable over IPv6 | Low | **Fixed**: IPv6 keyed by /64. Correct forwarded-header trust is a deployment item (`ARTIFACTS_TRUSTED_PROXIES`, Traefik `trustedIPs`) |
| 7 | Unicode digits in Content-Length caused a 500 | Info | **Fixed**: ASCII-only parse |
| 8 | Empty-body POST on the dashboard share route created a 7-day share | Info | **Fixed**: the dashboard route requires an explicit integer expiry |
| 9 | "Share only when asked" is enforced by tool wording, not policy | Info | **Accepted.** Sharing is a separate tool call. Every share is visible and revocable in the dashboard |

## Read-only security review (2026-10-07, iteration 3)

A native read-only reviewer subagent read the diff from pre-release development revision `623cf4e` (not in the public history) (console, auth, panel, Hermes host). No high or medium findings. Disposition:

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Login limit keyed on client address: a trusted-proxy `X-Forwarded-For` or many addresses buy unlimited bcrypt guesses | Low | **Fixed**: a global budget (120 verifications per minute) on top of the per-client 20; `test_login_attempts_share_a_global_budget` |
| 2 | bcrypt shares the threadpool with guest routes; no maximum cost | Low | **Fixed**: cost must be 10-15; at most two verifications run at once, the rest get 429 |
| 3 | Any API path ending in `/frame` skipped the panel-header and Origin checks | Low (latent) | **Fixed**: only `GET`/`HEAD /console/api/artifacts/<uuid>/frame`; `test_frame_exemption_is_only_the_preview_route` |
| 4 | The Hermes-hosted panel is same-origin with the dashboard, so the bridge is defence in depth, not a hard boundary | Info | **Accepted.** The panel is trusted code that renders text only (no HTML sinks); artifacts never run in it |
| 5 | Console origin may equal the guest origin | Info | **Accepted by product decision** (one host may serve both). Guest pages are trusted static code; artifacts stay opaque-origin |
| 6 | An origin with an explicit default port never matched the browser's `Host` | Info | **Fixed**: default ports are normalised away |

## Launch audit (2026-10-09)

Two independent read-only reviews (security; release and verification integrity) read the public tree at `a4e6690`. Neither found a critical, high or medium security issue. Fixed in this release unless marked otherwise:

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Library database and backups inherited the process umask (often world-readable) | Low | **Fixed**: created 0600 in a 0700 folder; existing files are left as they are, so `chmod 600` an older library yourself |
| 2 | Chunked bodies skipped the declared-length cap on the share routes (authenticated memory exhaustion) | Low | **Fixed**: 411 unless the route is the streamed, capped publish |
| 3 | Guest bridge kept idle connections and their threads forever | Low | **Fixed**: 10 s socket timeout |
| 4 | `?theme=` errors on guest routes answered 422 JSON before the link check and rate limit | Info | **Fixed**: unknown themes fall back to light after the link check |
| 5 | A title containing `{{token}}`-style text was substituted in the guest shell | Info | **Fixed**: one-pass substitution (no injection was possible: values are escaped or constrained) |
| 6 | `artifact_share` raised on infinite or non-numeric hours | Info | **Fixed**: `invalid_arguments` |
| 7 | Console API answers lacked `no-store` and `nosniff` | Info | **Fixed** |
| 8 | Agent file tools publish any readable file and download to any writable path, so a prompt-injected agent could publish its own `.env` | Low | **Accepted residual.** The agent already has general file tools; the plugin does not widen that. Shares remain explicit, visible and revocable |
| 9 | Uvicorn has no request-header timeout; 64 slow connections exhaust the service | Low | **Accepted with deployment rule**: never expose the service port directly; put a proxy that times out slow clients in both directions in front of the guest bridge (for Traefik, the entrypoint's `transport.respondingTimeouts.readTimeout` and `writeTimeout`; the default `writeTimeout` is unlimited). The bridge alone closes idle connections, not ones that send or read a byte at a time, and it buffers each response (up to the 10 MiB artifact limit), so the shipped unit caps its memory and threads. The example `deploy/compose.yml` also attaches the service to the shared `proxy` network, where other containers reach its port directly (still behind the machine credential); drop that network when your proxy talks only to the bridge |
| 10 | `ARTIFACTS_PUBLIC_URL` is not validated; Markdown re-renders on every view; the access log keeps private slugs of `/console/<slug>`; a trailing slash on a guest path gets the framework's 307 | Info | **Accepted**: operator configuration, rate-limited, operator-only log, no token information disclosed |
| 11 | The CSP and sandbox behaviour is proven by UAT on Chromium only | Gap | **Open**: Firefox and Safari are expected to fail closed but are not tested |
| 12 | `pip-audit` reports five Starlette 0.49.3 advisories (CVE-2026-48710, CVE-2026-54283, CVE-2026-54282, CVE-2026-48818, CVE-2026-48817), fixed only in Starlette 1.x, which needs a much newer FastAPI | Info | **Not reachable here**: the guards read the raw path and `Host` header, never `request.url`; the service uses no `request.form()`, `StaticFiles` or `HTTPEndpoint`, and runs on POSIX. Upgrade with the next FastAPI bump. `npm audit` reports no advisories |

## Residual limitations

- Basic authentication has no server-side logout. A browser keeps the credential until it is closed; rotate the hash to revoke every browser at once.
- The Hermes-hosted panel page (`dist/console/console.html`) is served by Hermes without a CSP of its own, like the rest of the dashboard. The standalone shell carries a strict CSP.
- The console login is one shared operator credential, not per-person accounts.

- CSP does not cover WebRTC, and browsers differ on DNS prefetch. Sandboxed artifacts can still compute, animate and beacon through such channels.
- A shared link is a bearer capability. Forwarding it grants access, and revocation cannot recall downloaded copies.
- Fullscreen belongs to the trusted wrapper. Artifacts cannot request it themselves.
- Browsers without `Sec-Fetch-Dest` (pre-2023 Safari) are redirected to the shell and therefore cannot show guest content. They fail closed.
- The service has no request-header timeout of its own. Do not expose its port to the internet; front it with a proxy that times out slow clients in both directions.
- The ASGI guards match the raw request path. The service never sets an ASGI `root_path`; do not run it under one (for example `uvicorn --root-path`), or the console and management guards would not see their prefixes.
- Agent file tools can publish any file the agent can read. A prompt-injected agent could publish a secret it can reach; sharing it still needs a separate, visible, revocable share.
