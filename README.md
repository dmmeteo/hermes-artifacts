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

This runs the service on loopback with a throwaway database and a credential generated on the spot. You need [uv](https://docs.astral.sh/uv/), `curl` and `jq`. uv installs Python 3.14 if it is missing.

```sh
git clone https://github.com/dmmeteo/hermes-artifacts.git
cd hermes-artifacts
uv sync --frozen

mkdir -p .local && chmod 700 .local
uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(32))' > .local/api-token
chmod 600 .local/api-token

export ARTIFACTS_PORT=8080
export ARTIFACTS_API_TOKEN_FILE="$PWD/.local/api-token"
export ARTIFACTS_DB="$PWD/.local/artifacts.db"
export ARTIFACTS_PUBLIC_URL="http://127.0.0.1:$ARTIFACTS_PORT"
uv run --frozen python -m app
```

In a second terminal in the same directory, publish an artifact, share it, open the link, then revoke it:

```sh
BASE=http://127.0.0.1:8080
AUTH="Authorization: Bearer $(cat .local/api-token)"

curl -fsS "$BASE/healthz"; echo                             # {"status":"ok"}

ID=$(printf '# Hello\n\nMy first artifact.\n' |
  curl -fsS -X POST "$BASE/internal/v1/artifacts?title=Hello&kind=markdown&source_profile=demo" \
    -H "$AUTH" -H 'Content-Type: text/markdown' --data-binary @- | jq -r .id)

SHARE=$(curl -fsS -X POST "$BASE/internal/v1/artifacts/$ID/shares" \
  -H "$AUTH" -H 'Content-Type: application/json' -d '{"expires_in_seconds": 3600}')
URL=$(echo "$SHARE" | jq -r .url); SHARE_ID=$(echo "$SHARE" | jq -r .share.id)
echo "$URL"                                                 # open it in a browser

curl -s -o /dev/null -w '%{http_code}\n' "$URL"             # 200
curl -fsS -X DELETE "$BASE/internal/v1/shares/$SHARE_ID" -H "$AUTH" | jq -r .share.status   # revoked
curl -s -o /dev/null -w '%{http_code}\n' "$URL"             # 404
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/internal/v1/artifacts"   # 401 without the credential
```

Stop the service with Ctrl-C. Everything it wrote is in `.local/` (git-ignored); delete that folder to start over.

### Turn on the standalone console

Create a login, then restart the service with the console enabled. The console origin must be `https`, except on loopback.

```sh
PASSWORD=$(uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(18))')
printf '%s\n' "$PASSWORD" | uv run --frozen python -m app.hash_password operator > .local/htpasswd
chmod 600 .local/htpasswd

ARTIFACTS_CONSOLE_ORIGIN="http://127.0.0.1:$ARTIFACTS_PORT" \
ARTIFACTS_CONSOLE_HTPASSWD_FILE="$PWD/.local/htpasswd" \
uv run --frozen python -m app
```

Open `http://127.0.0.1:8080/console/` and sign in as `operator` with the generated password (`echo "$PASSWORD"` in that terminal). Without the login, `/console/` answers 401. For your own password, run `uv run python -m app.hash_password operator`; it prompts without echo.

## Documentation

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
