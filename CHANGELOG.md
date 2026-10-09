# Changelog

## 0.1.0 (preview)

First public preview. Run from source; there is no published package or image.

- Service: a private artifact library (HTML, Markdown, SVG, PNG, WebP) with explicit, expiring, individually revocable guest links, a sandboxed guest viewer and a machine API.
- Standalone console behind one bcrypt-hashed operator login, and a one-command local launcher (`python -m app.local`).
- Hermes plugin: six agent tools, a packaged operating skill and an **Artifacts** dashboard tab sharing the console's panel.
- Launch audit fixes: owner-only database and backups, declared-length and chunked-body limits on authenticated routes, a guest-bridge socket timeout and capped unit, and the release-gate integrity fixes listed in [docs/SECURITY.md](docs/SECURITY.md).
- Dependencies: FastAPI 0.141 and Starlette 1.7, which close CVE-2026-48710, CVE-2026-48817, CVE-2026-48818, CVE-2026-54282 and CVE-2026-54283. A JSON share request now needs `Content-Type: application/json`.
- Release gates can run split across a browser host, a Docker host and a credential host without dropping a check ([CONTRIBUTING.md](CONTRIBUTING.md#integration-gates)).
