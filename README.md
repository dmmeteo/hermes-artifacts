# Hermes Artifacts

> **Preview.** This is an early public preview (0.1.0). The service, the standalone console and the Hermes plugin work and have automated tests, but the project has not been production-tested beyond one private pilot, and not every release gate has passed (see [Status](#status)). Interfaces and the database schema may change.

One shared, private library for the finished documents your agents produce, with explicit, expiring guest links.

Agents publish self-contained HTML, Markdown, SVG, PNG and WebP into one library. Nothing is public until someone creates a share link. Each link grants one immutable artifact, expires (seven days by default) and can be revoked on its own. The library belongs to no agent or profile: every agent configured for the same service sees the same library.

It has three parts:

- **Service** (`app/`, Python 3.14, FastAPI, SQLite). Stores artifacts and shares, applies the rendering policy, serves the guest viewer and the optional standalone console. It runs without Hermes, so guest links keep working when Hermes restarts.
- **Standalone console** (optional). The library UI at `<origin>/console/` behind one bcrypt-hashed login. Off unless you configure it.
- **Hermes plugin** (`plugin/`). Six agent tools, a packaged operating skill and an **Artifacts** tab in the [Hermes](https://hermes-agent.nousresearch.com/docs) dashboard. It holds the service credential server-side and stores no data.

Agents outside Hermes use the [machine API](docs/api.md) directly.

## Quickstart

You need [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/). uv installs Python 3.14 and the dependencies on first run.

```sh
git clone https://github.com/dmmeteo/hermes-artifacts.git
cd hermes-artifacts
uv run --frozen python -m app.local
```

Open the console URL it prints, `http://127.0.0.1:8080/console/`, and sign in as `operator` with the password shown in your terminal. The password is shown only on the first start and is never stored; `--new-password` replaces it. The service listens on loopback only. Stop it with Ctrl-C; the next run reuses the same library and login from `.local/` (git-ignored, private). The console starts empty: agents publish through the [Hermes plugin](docs/hermes.md) or the [machine API](docs/api.md) with the token in `.local/api-token`.

[Running locally](docs/local.md) covers the launcher's options and failure rules, the manual setup and a `curl` walkthrough of publish, share and revoke.

## Documentation

- [Running locally](docs/local.md): the one-command launcher, manual setup and a first walkthrough of the machine API.
- [Configuration](docs/configuration.md): every environment variable, the console login and its fail-closed rules.
- [Machine API](docs/api.md): publish, list, share and revoke over HTTP, with examples.
- [Hermes integration](docs/hermes.md): install the plugin, agent tools and the operating skill.
- [Deployment](docs/deploy.md): reverse proxy, container, guest-only bridge, backup and restore.
- [Security model](docs/SECURITY.md): threats, controls, review findings and residual limitations.
- [Architecture](ARCHITECTURE.md) and [diagram](architecture-diagram.md). Visual decisions: [DESIGN.md](DESIGN.md).
- [Contributing](CONTRIBUTING.md): development setup and checks.

## Guest links

- Publishing never shares. A share is a separate, explicit action from the console, the Hermes tab, the `artifact_share` tool or the machine API.
- A link is `<ARTIFACTS_PUBLIC_URL>/<token>`. The token is 22 base64url characters (128 random bits), unrelated to the artifact's id, slug or content. Only its SHA-256 is stored, so the URL is shown once.
- Anyone holding a link can view and download that one artifact until it expires or is revoked. Links can be forwarded.
- Expiry defaults to 7 days (`ARTIFACTS_DEFAULT_SHARE_SECONDS`) and is capped at 365 days (`ARTIFACTS_MAX_SHARE_SECONDS`).
- Revoking one link leaves other links to the same artifact working. It stops future access immediately but cannot recall copies already downloaded.
- Expired, revoked, unknown and malformed links all return the same 404 page.
- Artifacts are immutable. Republishing creates a new artifact, so a link never changes content.
- Artifact content runs in an opaque-origin sandboxed frame with a strict CSP that blocks network requests. CSP is not a complete network air gap (for example WebRTC); do not share content you would not want beaconed. See [docs/SECURITY.md](docs/SECURITY.md).

## Status

What has been exercised for this preview:

- Service unit and HTTP tests (`uv run --frozen pytest tests/service`) and plugin tests on Hermes's Python 3.11.
- Browser UAT and the concept gate (`scripts/uat.py`, `scripts/verify_concept.py`) against an isolated Hermes dashboard and a container build, on the development revision this preview was cut from.
- A private pilot of the service behind a reverse proxy with the guest-only bridge.

Known gaps:

- The full gate `scripts/verify.py` did not pass end to end: its recorded real-agent smoke is tied to development history that is not part of this repository, so that gate fails until a new smoke is run. See [CONTRIBUTING.md](CONTRIBUTING.md#integration-gates).
- No published container image, package or release. Run from source.
- One operator login for the console; no per-user accounts. Basic authentication has no server-side logout.
- An older database schema is refused, not migrated.
- Tested against Hermes 0.21.x only.

## License

[MIT](LICENSE). Report security issues privately as described in [SECURITY.md](SECURITY.md).
