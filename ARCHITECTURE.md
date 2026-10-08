# Architecture

The design of Hermes Artifacts and the decisions behind it. See `README.md` for operation and `docs/SECURITY.md` for the threat model and review disposition.

## Decision

Artifacts is one independent service with one library. It belongs to no agent or profile. The operator chooses its database path; `source_profile` is attribution and a filter, never ownership or a permission.

One panel browses the library. It has two hosts:

- **Inside the one Hermes board** (`/artifacts/<slug>`), through a thin trusted iframe host in the plugin. Hermes's session is the only login; no machine credential reaches the browser.
- **Standalone** on an origin the operator chooses (`ARTIFACTS_CONSOLE_ORIGIN`), served by the service at `/console/` behind one Basic login whose bcrypt hash comes from the environment or an htpasswd file. Off by default: embed-only stays a supported, safe configuration.

Guest links are explicit, expiring and revocable, at `<ARTIFACTS_PUBLIC_URL>/<token>`. The public host is a read-only sharing site. It may be the console's own host or a separate one; its root never lists artifacts.

Agents use the plugin tools in Hermes, or the machine API directly elsewhere. The plugin ships the operating skill `artifacts:operating`.

This is an original implementation. Any copied source requires a license check and preservation of its copyright and license notices.

## Hosting and authentication

An earlier design ran the library only as a page on the dashboard's own React, components and `authedFetch`. Non-Hermes operators had nothing, and the page could not leave Hermes. The current design moves the panel out:

```text
before                                       after
web/src/index.tsx   page on Hermes SDK       web/src/panel/      the panel: own React, own controls, host interface
web/src/api.ts      authedFetch wrapper        host.ts           standaloneHost (fetch + Basic) | hermesHost (postMessage)
                                               app.tsx, ui.tsx, api.ts, frame.ts, console.html
                                             web/src/host/hermes.tsx  dashboard tab: iframe + request bridge + URL sync
                                             web/src/protocol.ts      the messages between them
app/main.py         internal API, guest      app/main.py         + one set of operations mounted twice
                                             app/console.py      NEW ConsoleGuard, BasicAuth, /console routes
                                             app/config.py       + ConsoleSettings, fail-closed validation
                                             app/console/        built panel (same bytes as plugin/dashboard/dist/console)
plugin/__init__.py  tools                    plugin/__init__.py  tools + register_skill("operating")
                                             plugin/skills/operating/SKILL.md
```

How a request travels:

- Standalone: browser → `/console/api/...` with the browser-held Basic credentials and `X-Artifacts-Console: 1` → `ConsoleGuard` (console enabled, `Host` is the configured origin, bcrypt check with a short keyed cache and a per-client attempt limit, panel header, exact `Origin` for writes) → the same operation functions the machine API uses.
- Hermes: panel iframe → `postMessage` to the host tab (same origin, its own window only) → allowlist of eight method/path shapes → `SDK.authedFetch('/api/plugins/artifacts/...')` → `plugin_api.py` → machine API with the server-side token. The response body returns to the panel as bytes. No token, session or cookie value is ever in a message.

Why an iframe and a bridge, not an iframe pointing at a plugin route: Hermes serves plugin static files without authentication (`hermes_cli/web_routers/dashboard_ui.py`, suffix allowlist includes `.html`), but its plugin API routes need the `X-Hermes-Session-Token` header in loopback mode and a cookie only in gated mode. An iframe navigation cannot add a header, so the static panel asks its host to fetch for it. That works in both Hermes auth modes.

Previews differ only in transport. Standalone, the panel frames `/console/api/artifacts/<id>/frame`, a wrapper document served with the content CSP and `sandbox allow-scripts`; the shell's own CSP (`script-src 'self'`) would otherwise be inherited by a `srcdoc` frame. In Hermes, the panel builds the same wrapper as a `srcdoc` from the JSON document the adapter returns. Both block artifact self-navigation with `frame-src 'none'` and report it.

Tradeoffs we accepted:

- **The panel bundles React** (about 200 KB minified) instead of borrowing the dashboard's. That is the price of running without Hermes and of not depending on Hermes globals. Inside Hermes it adopts the host's colour tokens through the host message; the dashboard's web fonts are not loaded in the iframe, so the panel uses system fonts there.
- **Two wrapper builders** (`app/render.py` and `web/src/panel/frame.ts`) for one policy. Each is exercised by browser navigation-blocking checks; unifying them would force the dashboard path through an authenticated URL it cannot load.
- **Basic authentication** rather than sessions: no cookie, CSRF token store, user table or logout. The browser holds the credential; the console defends writes with the panel header plus an exact `Origin` check, and refuses framing. Logging out means closing the browser or changing the hash. One operator login; multiple accounts are out of scope.
- **bcrypt per request is slow by design**, so a verified header is cached for five minutes under an HMAC with a per-process key; failed verifications are limited to 20 per client per minute.

Not added: a session store, an identity platform, an ORM, a second admin app, a message bus or workers. No schema change: authentication is configuration, not data.

## Hermes plugin contract

The plugin follows Hermes's documented plugin and dashboard-extension contracts ([Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/), [Extending the Dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard)): a `dashboard/manifest.json` declaring the tab path, bundle entry and backend API; user-plugin backends imported only when the plugin is enabled and mounted under `/api/plugins/<name>`; plugin API routes behind the dashboard session. `scripts/verify.py` runs the relevant Hermes contract tests from a Hermes checkout in an isolated home. Developed and tested against Hermes 0.21.x on Python 3.11.

## Components and ownership

### Hermes plugin

The dashboard tab is a thin host for the shared panel (see Hosting and authentication). It uses the dashboard's existing session through `authedFetch` and passes the host theme's colour tokens to the panel. It does not introduce another password form.

`dashboard/manifest.json` registers the route `/artifacts/*`, so the library is `/artifacts` and one artifact is `/artifacts/<slug>`. The page resolves the slug once through `GET /slugs/{slug}`, the only slug-to-artifact boundary; every later call uses the artifact id. An unknown slug shows not found. `dashboard/plugin_api.py` is a FastAPI adapter because that is the host's required contract. It forwards requests to the artifact service through a bounded, authenticated client. It never owns the artifact database.

A small agent tool group supports publishing, listing, creating shares, and revoking shares. Publishing saves privately. Creating a share is a separate explicit action. Tool availability follows plugin enablement and service configuration. The adapter obtains source profile and session context from Hermes instead of guessing them from an empty profile string.

### Artifact service

One Python 3.14 FastAPI application in `app/` owns the database and all artifact operations. One ASGI process is enough. Do not add an event bus, workers, Redis, a second database, or a separate renderer service.

Its management endpoints are under `/internal/v1/`. Every management request requires a service credential, checked by an ASGI guard before any body is read and again by the router. That credential stays on the server and never reaches dashboard JavaScript or an artifact. One trusted operator and one shared library. Profile is provenance and a filter, not a promise of separate user accounts. The standalone console (`/console/`) has its own guard described above; it never accepts the machine credential, and the machine API never accepts the console login.

The reverse proxy exposes only public viewer routes on the guest host. It does not forward `/internal/`, debug endpoints, the OpenAPI schema, or management routes. The application still authenticates management requests if a proxy rule is misconfigured. Keep its port off the public host interface.

### Public viewer

`GET /<share-token>` renders the trusted viewer shell. `GET /<share-token>/content` renders the artifact in an isolated frame. Only GET and HEAD are public. The viewer cannot list the library, publish, delete, or manage shares.

The URL token authorizes exactly one immutable artifact. A shared link can be forwarded by its recipient. The UI must state this plainly. Expiry and revocation are checked on every shell and content request. A revoked share returns the same not-found response as an unknown token.

A separate Hermes Desktop adapter is not part of this first build. Desktop and Dashboard have different SDKs. A later Desktop adapter can use the same service without redesigning storage.

## Data model

Use SQLite through parameterized SQL in the standard library. Store small artifact payloads as BLOBs in the same database as their metadata. This gives atomic publication and a single consistent backup without file-to-database reconciliation. A configurable initial maximum of 10 MiB per artifact keeps this choice bounded.

`Artifact` contains:

- `id`, a UUIDv4 used by the private library.
- `title`, `kind`, `created_at`, `source_profile`, and optional `source_session_id`.
- `content_sha256`, `byte_length`, and `content`, the immutable source bytes.

`Share` contains:

- `token_hash`, the SHA-256 digest of a randomly generated bearer token.
- `artifact_id`, a foreign key to `Artifact`.
- `created_at`, `expires_at`, and optional `revoked_at`.

The URL token is 128 random bits encoded as 22 base64url characters. It is not the artifact UUID and not a content hash. Publish keeps the artifact private. Share generates the URL and stores only its token digest. A share remains bound to the same bytes.

No editable revisions in the initial release. Republishing creates a new artifact. Existing links never silently change content. Each artifact can have independently revocable shares. Every share expires: seven days by default, at most 365 days, both configurable by the operator.

Enable foreign keys, WAL, short transactions, and a bounded busy timeout. Run blocking database work off the ASGI event loop. Use SQLite's online backup API rather than copying only a live `.db` file while its WAL is active. Restore testing is an acceptance gate.

## User flows

### Publish and browse

1. An agent sends a bounded local file to the plugin client.
2. The service validates kind, size, and provenance, then commits one immutable artifact.
3. The agent receives the private artifact ID and dashboard location. No guest share exists yet.
4. `/artifacts` lists every saved artifact in the operator library, newest first. Search, kind, and profile filters use paginated metadata queries. Listing never loads every payload.

Initial kinds are HTML, Markdown, SVG, PNG, and WebP. Interactive HTML must be self-contained. Arbitrary React source, npm dependency installation, ZIP applications, remote asset fetching, editing, and comments are not initial features.

### View and expand

Selecting an artifact opens a viewer that fills the available workspace. A compact toolbar provides Back, Fullscreen, Download, and Share. A tiny preview is not the default layout.

The owner preview obtains bytes through the authenticated plugin route. It creates an opaque-origin sandboxed frame without minting a durable guest share. Fullscreen uses a user gesture on the trusted outer viewer. If the browser lacks the Fullscreen API, provide a viewport-sized overlay owned by the plugin. The overlay avoids the dashboard shell's ordinary width limit without changing global themes or core CSS. Exit restores the same selection and library position.

### Share and revoke

The owner explicitly creates a share and chooses its expiry. The service returns `<ARTIFACTS_PUBLIC_URL>/<share-token>`. The guest viewer fills the browser window. A bottom-centre ellipsis menu offers Download and the link's expiry, without access to the library. Revocation stops future retrieval immediately, but cannot erase bytes that a recipient has already downloaded.

## Security boundaries

The plugin is trusted application code. Generated artifact content is not.

- Never insert artifact HTML or SVG into the Hermes DOM, execute it as a plugin, or give it Hermes API credentials.
- Use `sandbox="allow-scripts"` without `allow-same-origin`, top navigation, forms, or popups. Fullscreen belongs to the trusted wrapper, not to arbitrary artifact code.
- Serve public artifact content with a response CSP and sandbox policy. Enforce the equivalent policy in owner previews. Markdown produces sanitized HTML. Render SVG inside the same isolated frame, not as inline dashboard markup.
- Permit embedded scripts and styles needed by self-contained HTML. Block network fetches and external assets by default. Do not call CSP a complete network air gap. Frame navigation and browser behavior need explicit browser tests.
- Downloads use attachment responses with controlled names and MIME types. Raw source must not execute directly under the dashboard origin.
- Service secrets remain in the secure secret sink. Browser JavaScript uses Hermes session authentication only.
- Share pages use `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. Use no analytics or third-party assets. Redact capability paths in proxy and application access logs.
- An ID is not an access check. Verify the credential or share before loading payload bytes.
- Default to fail-closed behavior for malformed, expired, revoked, and unknown shares. Apply bounded body sizes, timeouts, and public request rate limits.

## Technologies

- Python 3.14 and FastAPI (uvicorn) for the independent service, locked in `uv.lock`. One framework on both sides: the host adapter is also FastAPI because that is the Hermes loader contract.
- Python 3.11-compatible FastAPI and the host's existing runtime for the thin Hermes adapter. Do not force Hermes onto Python 3.14.
- SQLite for artifact bytes, metadata, and shares. No ORM in the initial release.
- TypeScript and React for the shared panel, bundled by esbuild into IIFE bundles: the panel (own React) and the thin Hermes host tab (dashboard SDK).
- A small HTML, CSS, and JavaScript viewer for guest shares. No second React application or login system.
- A lean container or a plain process, a persistent SQLite file, and any TLS reverse proxy that routes only guest paths.

## Project structure (implemented)

```text
hermes-artifacts/
  app/                      independent service (Python 3.14, FastAPI)
    domain.py               Artifact and Share shapes, kinds, payload validation
    store.py                SQLite schema, queries, immutability trigger, online backup
    render.py               content CSP and per-kind rendering (HTML, Markdown, SVG, PNG, WebP)
    main.py                 transport: management API, public viewer, guards, rate limit, log redaction
    console.py              optional standalone console: login guard, Origin/header checks, /console routes
    config.py, __main__.py  settings from env (console fails closed), uvicorn entrypoint
    hash_password.py        `python -m app.hash_password USER` prints an htpasswd line
    local.py                `python -m app.local`: private .local/ state, then the service on loopback
    console/                built shared panel served at /console/
    backup.py               `python -m app.backup DEST`
    viewer/                 guest shell (plain HTML/CSS/JS)
  plugin/                   Hermes plugin, installed as <HERMES_HOME>/plugins/artifacts
    plugin.yaml, __init__.py, tools.py, client.py
    skills/operating/SKILL.md   packaged skill artifacts:operating
    dashboard/manifest.json, plugin_api.py, dist/index.js (iframe host), dist/console/ (shared panel)
  web/src/panel/            the shared panel (TypeScript + React), built by web/build.mjs
  web/src/host/             the Hermes dashboard host adapter (uses the Hermes SDK)
  scripts/                  runtime lever, UAT, host dispatch and skill E2E, agent smoke, container smoke, verify, verify_concept
  .claude/skills/verify-hermes-artifacts/   project verification recipe and helpers
  tests/service, tests/plugin, tests/fixtures
  deploy/                   compose file, guest-only bridge, systemd and Traefik templates
  Dockerfile
```

`domain.py` owns the data shapes and their rules; `store.py` owns persistence; `render.py` owns content policy; `main.py` is the only transport layer. The plugin never owns the database.

The dashboard preview runs the artifact in an inner frame of a trusted wrapper frame (both `sandbox="allow-scripts"`, opaque origin). The wrapper carries the service's content CSP, including `frame-src 'none'`, which governs the artifact frame's own navigations. The guest shell does the same with `frame-src 'self'`, and `/<token>/content` renders only as an iframe destination (`Sec-Fetch-Dest: iframe`); a top-level open is redirected to the shell.

## Alternatives rejected

Putting the entire service inside the Hermes process is cheaper initially, but couples public shares and storage operations to gateway restarts and the host's Python dependencies. A thin adapter plus one independent service earns its extra boundary.

A separate standalone administration app was first rejected, then reconsidered: operators without Hermes need the library. The answer is the same panel served by the same service behind a configured login, not a second application. Serving the console from the public guest host's root remains rejected; it lives at `/console/` and only on its configured origin.

## Acceptance gates

The implementation is checked by `tests/service`, the plugin tests, `scripts/uat.py`, `scripts/verify_concept.py`, the container smoke and a bounded real-agent smoke, all run together by `scripts/verify.py` (see `CONTRIBUTING.md`). They cover:

- A real `/artifacts` tab loads in an isolated Hermes dashboard without source patches.
- Publish, list, and owner preview work while no guest share exists.
- Owner and guest viewers fill their containers on desktop and phone. Fullscreen and its fallback preserve viewer state.
- Untrusted scripts cannot read Hermes cookies, call management APIs with credentials, navigate the top window, or run in the Hermes DOM.
- Anonymous callers cannot list artifacts or perform management writes.
- Expiry and revocation apply to content requests as well as the viewer shell.
- Restart preserves artifacts and shares. A backup restores both into an isolated service.
- Public shares still work during a Hermes gateway restart.
- The service runs under Python 3.14 and the thin plugin imports under Hermes's Python 3.11.
