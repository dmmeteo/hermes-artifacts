# Standalone console

Promise: with `ARTIFACTS_CONSOLE_ORIGIN` set, `/console/` serves the shared panel to one operator whose bcrypt hash comes from `ARTIFACTS_CONSOLE_PASSWORD_HASH` or `ARTIFACTS_CONSOLE_HTPASSWD_FILE`. Without it the console does not exist (embed-only). Bad config stops the service.

Code: `app/config.py` (`console_from_env`), `app/console.py` (`ConsoleGuard`, `BasicAuth`, `console_router`), `app/console/` (built panel), `web/src/panel/host.ts` (`standaloneHost`).

Drive: Chromium context with `http_credentials`, open `/console/`, wait for `.ha-row`, open a row, wait for `[data-testid=artifact-frame]`. Without credentials expect 401 and a `Basic` challenge; API calls need `X-Artifacts-Console: 1`; writes need `Origin` equal to the configured origin.
