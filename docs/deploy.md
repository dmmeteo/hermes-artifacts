# Deployment

Artifacts is one process and one SQLite file. Run it as a system service or a container, keep its listener private, and let a reverse proxy expose only what guests need. Replace `share.example.com` and `artifacts.example.com` below with your hosts.

## What to expose

| Surface | Path | Exposure |
|---|---|---|
| Guest links | `/<token>`, `/<token>/content`, `/<token>/download`, `/_viewer/viewer.css`, `/_viewer/viewer.js` | Public host, `GET`/`HEAD` only |
| Standalone console | `/console/...` | Its own origin over HTTPS, only if enabled |
| Machine API | `/internal/v1/...` | Never public. Loopback or private network for Hermes and agents |

The service authenticates the machine API and the console itself, but a proxy that never routes `/internal/` is a second line of defence. Turn off proxy access logs for the guest host: every guest path is a capability URL. Do not let the proxy add identity headers; the service ignores them.

Set `ARTIFACTS_PUBLIC_URL` to the guest host so returned links point there, and `ARTIFACTS_TRUSTED_PROXIES` to the proxy's address so rate limiting sees real client addresses.

## As a system service

```sh
uv sync --frozen --no-dev
ARTIFACTS_API_TOKEN_FILE=/etc/hermes-artifacts/api-token \
ARTIFACTS_DB=/var/lib/hermes-artifacts/artifacts.db \
ARTIFACTS_PUBLIC_URL=https://share.example.com \
ARTIFACTS_PORT=8080 \
uv run --frozen --no-dev python -m app
```

Wrap this in a systemd unit or your supervisor of choice, running as a dedicated user that owns the database directory and can read the 0600 credential file.

## As a container

`Dockerfile` builds a non-root image (uid 10001) with the database in the `/data` volume. `deploy/compose.yml` is an example: read-only root filesystem, dropped capabilities, memory and pid limits, the credential as a Compose secret, and the port published on host loopback only.

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > api-token && chmod 600 api-token
REVISION=$(git rev-parse --short HEAD) \
ARTIFACTS_API_TOKEN_FILE=$PWD/api-token \
ARTIFACTS_PUBLIC_URL=https://share.example.com \
docker compose -f deploy/compose.yml up -d --build
```

The example joins an external Docker network named `proxy` for the reverse proxy; adjust it to yours. No image is published; build from source.

## Guest-only bridge

When the service runs on host loopback and the reverse proxy runs in Docker, the proxy cannot reach `127.0.0.1`. `deploy/guest_bridge.py` (stdlib only, Python 3.11+) closes that gap without exposing the service:

- It listens on an address the proxy can reach, typically the Docker host gateway `172.17.0.1`.
- It forwards only `GET`/`HEAD` of the guest paths above. Everything else gets the service's own "link unavailable" page; other methods get 405.
- It forwards an allowlist of request headers (never `Authorization` or `Cookie`) and writes no access log.
- It trusts `X-Forwarded-For` only from `BRIDGE_TRUSTED_PROXIES`, and `CF-Connecting-IP` only when the peer is a Cloudflare edge.

Templates:

- `deploy/systemd/hermes-artifacts-guest.service`: a user unit. Replace `@PYTHON@` with a Python 3.11+ interpreter and `@RELEASE@` with a directory holding a fixed copy of `guest_bridge.py`; adjust the addresses.
- `deploy/traefik/hermes-artifacts.yml`: a Traefik file-provider route for `share.example.com` to the bridge. It strips `Cookie` and `Authorization`, sets `Referrer-Policy: no-referrer`, `noindex` and HSTS, and disables access logs for the route.

Check after deploying: `https://share.example.com/` and `https://share.example.com/internal/v1/artifacts` show the unavailable page, and a fresh share link opens without any login.

Other proxies (nginx, Caddy) work the same way: route only the guest paths, drop credentials and cookies, disable access logs for the host.

## Backup and restore

`app.backup` uses SQLite's online backup API, so it is safe while the service runs. It refuses to overwrite an existing file.

```sh
ARTIFACTS_DB=/var/lib/hermes-artifacts/artifacts.db \
  uv run --frozen --no-dev python -m app.backup /backups/artifacts-$(date +%F).db
# container:
docker exec hermes-artifacts python -m app.backup /data/backup-$(date +%F).db
```

To restore, stop the service, replace the database file with the backup, remove any stale `artifacts.db-wal` and `artifacts.db-shm` beside it, and start the service. Artifacts, shares, expiry and revocation state all restore; existing links work again.

Copying the live `.db` file directly can miss data still in the WAL; use `app.backup`.

## Upgrades

The schema version is checked at startup. A database with a different schema version is refused, not migrated. Back it up before upgrading. During the preview, a schema change may require starting a new database.
