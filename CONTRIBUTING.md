# Contributing

Thanks for helping. This is a preview, so open an issue before a large change to agree on the approach. Keep changes small, keep the existing security semantics, and add or update a test for any behavior change. Report vulnerabilities privately ([SECURITY.md](SECURITY.md)), not in issues.

## Layout

```text
app/        service: domain.py (shapes, rules), store.py (SQLite), render.py (content policy),
            main.py (machine API, guest viewer), console.py (standalone console), config.py
app/console/, plugin/dashboard/dist/   built panel bundles (committed; rebuild from web/)
plugin/     Hermes plugin: tools, client, dashboard adapter, packaged skill
web/src/    panel (React + TypeScript) and the Hermes dashboard host
deploy/     container, proxy and guest-bridge templates
scripts/    UAT, concept and full gates, container and agent smokes
tests/      service tests (Python 3.14), plugin tests (Hermes Python 3.11), fixtures
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains the boundaries; [docs/SECURITY.md](docs/SECURITY.md) lists the controls each change must keep.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (installs Python 3.14), and Node.js 20+ with npm if you change `web/`.

```sh
uv sync --frozen
uv run --frozen pytest tests/service
```

## Checks every change should pass

These need nothing beyond the repository:

```sh
uv lock --check                         # lockfile matches pyproject.toml
uv run --frozen pytest tests/service    # service, console and guest-bridge tests
cd web && npm ci && npm run build       # typecheck and rebuild both panel bundles
git diff --exit-code -- app/console plugin/dashboard/dist   # committed bundles match the source
```

If you change `web/src`, commit the rebuilt bundles. `app/console/` and `plugin/dashboard/dist/console/` must stay byte-identical.

## Integration gates

These drive the real app and need more of the environment. They write only under `ARTIFACTS_SCRATCH` (default `.scratch/` in the checkout, git-ignored), use loopback ports and an isolated `HERMES_HOME`, and refuse to run against a live Hermes home.

| Command | Needs |
|---|---|
| `<hermes-venv>/bin/python -m pytest tests/plugin -o addopts= --rootdir tests/plugin` | A Hermes checkout's Python 3.11 venv (`HERMES_CHECKOUT`, default `~/.hermes/hermes-agent`) |
| `uv run --frozen python scripts/uat.py [--executable /path/to/chrome]` | Hermes checkout, Chromium or Chrome (Playwright), built bundles |
| `uv run --frozen python scripts/verify_concept.py` | Same as UAT; checks standalone and embedded hosts, auth negative controls, the packaged skill |
| `uv run --frozen python scripts/container_smoke.py` | Docker; labels and removes its own resources |
| `uv run --frozen python scripts/agent_smoke.py` | A Hermes home or profile (`HERMES_SMOKE_PROFILE`, default `~/.hermes`) whose default model uses a Codex subscription credential, and authority to spend up to 15 agent turns. The credential is resolved read-only and never written |
| `uv run --frozen python scripts/verify.py` | All of the above, plus the Hermes checkout's contract tests |

`scripts/verify.py` runs every gate in order and fails if any fails. One gate checks that a recorded agent smoke matches the current plugin and backend source; it fails until `agent_smoke.py` has been run on the current revision. The project's own driving recipe is `.claude/skills/verify-hermes-artifacts/SKILL.md`.

When the browser host should not run Docker or hold the agent credential, split the same gates without dropping any:

- Run `scripts/container_smoke.py` on the Docker host in the same clean checkout. It keeps its report, with the full revision, in `$ARTIFACTS_SCRATCH/evidence/container-smoke/report.json`. Then run `scripts/verify.py --container-smoke-report <that file>` on the browser host; the gate passes only for a passing report of the same clean revision.
- Run `scripts/agent_smoke.py` on the browser host with `ARTIFACTS_SMOKE_EXCHANGE=<dir>`. It starts the service, dashboard and browser there, writes `<dir>/request.json` and waits for the agent's outcome. On the credential host, forward the service port to the same loopback port, copy `request.json` into a local folder and run `scripts/agent_smoke.py --runner <folder>` (this host needs the Hermes checkout and its venv too); copy the resulting `outcome.json` back into `<dir>` under another name and rename it there, so the waiting smoke never reads a partial file. The credential never leaves the credential host, and both hosts must be at the same clean revision.

Do not mark a gate as passing that you did not run. In a pull request, list the commands you ran and their results.

## Style

Match the surrounding code. Python is typed and standard-library first; the service has four runtime dependencies and should stay small. Commit messages use `type(scope): subject`.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
