# Configuration

The service reads its configuration from the environment at startup (`app/config.py`, `app/__main__.py`). It refuses to start on an invalid or unsafe setting rather than falling back.

To try it on your own machine without setting any of this, use the launcher in [local.md](local.md).

## Service

| Variable | Default | Meaning |
|---|---|---|
| `ARTIFACTS_API_TOKEN` or `ARTIFACTS_API_TOKEN_FILE` | required | Machine credential for `/internal/v1`, at least 32 characters. Prefer the file form (mode 0600). Generate it with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. |
| `ARTIFACTS_DB` | `data/artifacts.db` | SQLite file (WAL mode). Schema version 2; an older database is refused, not migrated. |
| `ARTIFACTS_PUBLIC_URL` | `http://127.0.0.1:8080` | Base of the share URLs the service returns. Set it to your public guest host, for example `https://share.example.com`. |
| `ARTIFACTS_HOST`, `ARTIFACTS_PORT` | `127.0.0.1`, `8080` | Listener. Keep it on loopback or a private network; put a reverse proxy in front for guests. |
| `ARTIFACTS_MAX_BYTES` | `10485760` (10 MiB) | Per-artifact size limit. |
| `ARTIFACTS_DEFAULT_SHARE_SECONDS` | `604800` (7 days) | Expiry when a share request names none. |
| `ARTIFACTS_MAX_SHARE_SECONDS` | `31536000` (365 days) | Longest allowed expiry. |
| `ARTIFACTS_PUBLIC_RPM` | `120` | Guest requests per minute per client address (IPv6 grouped by /64). |
| `ARTIFACTS_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-For` names the client address (rate limiting only, never identity). |
| `ARTIFACTS_MAX_CONNECTIONS` | `64` | Concurrent connection limit. |

## Standalone console

| Variable | Default | Meaning |
|---|---|---|
| `ARTIFACTS_CONSOLE_ORIGIN` | unset | Turns the console on at `<origin>/console/`, for example `https://artifacts.example.com`. Unset means there is no `/console` at all. Must be `https`, except a loopback address. A bare origin: no path, query or credentials. |
| `ARTIFACTS_CONSOLE_USER` + `ARTIFACTS_CONSOLE_PASSWORD_HASH` | unset | The one console login. The hash must be bcrypt (`$2y$`, `$2b$` or `$2a$`) with cost 10 to 15. |
| `ARTIFACTS_CONSOLE_HTPASSWD_FILE` | unset | Alternative to the pair above: a file with exactly one `user:bcrypt-hash` line. |

Create the login with either tool. Both print a `user:hash` line:

```sh
uv run --frozen python -m app.hash_password operator > /etc/hermes-artifacts/htpasswd   # prompts without echo, 12-72 bytes
htpasswd -nBC 12 operator                                                       # Apache tools, compatible
```

Keep the file mode 0600 and owned by the service user. In Compose files, escape `$` in a hash as `$$`.

The service refuses to start when:

- the origin is set without a login, or a login is set without an origin;
- the hash is plaintext, not bcrypt, or its cost is outside 10 to 15;
- both the htpasswd file and the user/hash pair are set;
- `ARTIFACTS_CONSOLE_PASSWORD` is set at all (plaintext is never accepted);
- the origin uses `http` on a non-loopback host, or carries a path, query or credentials.

There is no user table and no unauthenticated fallback. To revoke every signed-in browser at once, change the hash and restart.

The console answers only when the request's `Host` equals the origin's host, so a separate guest host never serves it. It may share a host with guest links: set `ARTIFACTS_PUBLIC_URL` to the same origin. The root `/` lists nothing either way.

## Hermes plugin

Set in `<HERMES_HOME>/.env` (mode 0600). See [hermes.md](hermes.md).

| Variable | Meaning |
|---|---|
| `HERMES_ARTIFACTS_URL` | Service URL reachable from Hermes, for example `http://127.0.0.1:8080`. |
| `HERMES_ARTIFACTS_TOKEN` | The same machine credential as the service's `ARTIFACTS_API_TOKEN`. |

Without them the tools report `not_configured` and the dashboard tab shows a setup hint.

## Guest-only bridge

Optional; see [deploy.md](deploy.md#guest-only-bridge). `BRIDGE_LISTEN` (`host:port`), `BRIDGE_UPSTREAM` (`http://<loopback>:<port>` of the service) and `BRIDGE_TRUSTED_PROXIES` (comma-separated CIDRs of the reverse proxy's network).
