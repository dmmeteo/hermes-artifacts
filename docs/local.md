# Running locally

Two ways to run the service on your own machine: the one-command launcher from the [README quickstart](../README.md#quickstart), or the same setup by hand. Both listen on loopback only. For a server behind a reverse proxy, see [deploy.md](deploy.md).

## The launcher

```sh
uv run --frozen python -m app.local            # options: --port 8080, --dir .local, --new-password
```

It runs the real service (`python -m app`) in the foreground on `127.0.0.1` with the standalone console turned on, and adds nothing else: no daemon, no published port, no plugin installation.

**First start.** It creates `.local/` in the checkout (git-ignored, mode 0700) with:

| File | Contents |
|---|---|
| `api-token` (0600) | A random machine credential for the [machine API](api.md) and the [Hermes plugin](hermes.md). |
| `htpasswd` (0600) | The console login `operator` and a bcrypt hash (cost 12) of its password. |
| `artifacts.db` | The library, created by the service. |

In a terminal, the console password is generated and printed once. It is not stored anywhere, so save it in your password manager. When stdin is not a terminal, the launcher reads the password (12-72 bytes) from the first line of stdin instead and never prints it.

The folder is built under a temporary name and renamed into place, so an interrupted first start leaves nothing half-made.

**Later starts** reuse `.local/` as it is: the same library, credential and login. The launcher never resets a password on its own. If you lost it, `--new-password` replaces only the login and keeps the library and machine credential.

**It refuses to start, and changes nothing, when:**

- the port is in use (pick another with `--port`);
- `.local/` exists but is missing `api-token` or `htpasswd`, for example after the old manual setup below;
- `.local/` is not a directory you own with mode 0700, or one of its files is not a regular file with mode 0600;
- a saved file is unusable (for example an `htpasswd` that is not one `user:bcrypt` line).

**Start over** by stopping the service and deleting `.local/`. That deletes the library too.

Open the console at exactly the URL it prints, `http://127.0.0.1:<port>/console/`. The console answers only on its configured host, so `localhost` gets a 404. Basic authentication has no server-side logout: close the browser, or run `--new-password` and restart, to sign out every browser.

## Manual setup

The same result with explicit environment variables. Every variable is described in [configuration.md](configuration.md).

```sh
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

Without the console variables the service is embed-only and `/console` does not exist. To turn the console on, create a login and restart with two more variables. The console origin must be `https`, except on loopback.

```sh
uv run --frozen python -m app.hash_password operator > .local/htpasswd   # prompts without echo
chmod 600 .local/htpasswd

ARTIFACTS_CONSOLE_ORIGIN="http://127.0.0.1:$ARTIFACTS_PORT" \
ARTIFACTS_CONSOLE_HTPASSWD_FILE="$PWD/.local/htpasswd" \
uv run --frozen python -m app
```

Without the login, `/console/` answers 401.

## Try the machine API

With the service running (either way), use a second terminal in the checkout to publish an artifact, share it, open the link and revoke it. This needs `curl` and `jq`.

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

Publishing never creates a share; the share link exists only after the explicit `POST .../shares`. The full API is in [api.md](api.md).
