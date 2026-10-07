---
name: verify-hermes-artifacts
description: Use to launch, health-check, drive and clean up a disposable Hermes Artifacts stack (service, standalone console, isolated Hermes dashboard) and capture proof that a change works.
---

# Verify Hermes Artifacts

Drives the real app: one artifact service with its standalone console, plus an isolated Hermes dashboard with the plugin installed. Everything runs on loopback under an owned scratch root. Secrets are synthetic and never printed.

Parameters: `ARTIFACTS_SCRATCH` (owned scratch; required for anything but the default lane), `HERMES_CHECKOUT` (Hermes source with its `venv`; read-only), `--checkout` (this repository; defaults to the skill's own checkout), `--root` (runtime folder; defaults to `$ARTIFACTS_SCRATCH/runtime/recipe`). The helper refuses `~`, `~/.hermes`, a profile root, the Hermes checkout and any folder holding `config.yaml` or `state.db`.

Helper: `uv run --frozen python .claude/skills/verify-hermes-artifacts/scripts/recipe.py STEP`.

## Launch

`recipe.py launch` builds nothing. Build first if `web/src` changed: `cd web && npm ci && npm run build`. Launch writes a 0600 machine token and an htpasswd line from a synthetic password, starts the service with `ARTIFACTS_CONSOLE_ORIGIN=http://127.0.0.1:<port>`, installs `plugin/` into `<root>/hermes-home` and starts `hermes dashboard --isolated`. Ready when it prints the URLs; `<root>/state.json` records them and the process ids. Teardown is Cleanup.

## Doctor

`recipe.py doctor`: read-only. Service `/healthz` is 200; `/console/` and `/internal/v1/artifacts` are 401 without credentials; the dashboard lists the `artifacts` plugin and serves `dist/console/console.html`; `/api/plugins/artifacts/artifacts` is 401 without the Hermes session. Run it before the first drive and after any failure.

## Drive

`recipe.py drive` exercises each feature in `features/` on the real surfaces:

- Publish two artifacts with different `source_profile` through the machine API.
- Run `scripts/host_skill.py` under the Hermes interpreter: `skills_list`, `skill_view('artifacts:operating')`, tool descriptions, a publish and a list.
- Create, open, revoke and re-open a guest link through the console API (Basic login, panel header, exact Origin).
- Chromium with the console login on `/console/` (`.ha-row` rows), then the dashboard `/artifacts`, whose panel is the iframe `[data-testid=artifacts-panel]` (same `.ha-row` rows inside it).

Selectors inside the panel (both hosts): `.ha-row`, `[data-testid=viewer]` with `data-mode`, `[data-testid=artifact-frame]`, `dialog.ha-share[open]`, `button:has-text('Create link')`, `input[aria-label='Share link']`, `.ha-share__revoke`, `dialog.ha-confirm[open]`. Guest viewer: `#menu-toggle`. The artifact itself is a sandboxed frame inside a wrapper frame; `scripts/uat.py` `ArtifactFrame` shows how to read and click it through CDP.

Deeper gates: `scripts/verify_concept.py` (AC1-AC8, both hosts, negative controls) and `scripts/uat.py` (full dashboard journey).

## Evidence

`drive` writes `$ARTIFACTS_SCRATCH/evidence/recipe/<stamp>/report.json` and `standalone-library.png`, `embedded-library.png`. Check `passed` and that both row counts match. Service and dashboard logs are in `<root>/logs` until cleanup; copy what you need first.

## Cleanup

`recipe.py cleanup` kills only the process groups recorded in `state.json` and deletes `--root`. It never touches evidence. Confirm the evidence folder still exists afterwards.

## Feature map

`features/README.md` indexes one file per user-facing feature: what it promises, where it lives, how to drive it.
