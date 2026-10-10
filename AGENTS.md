# AGENTS.md

Cold-start map for agents working in this repository. [README.md](README.md) owns the product purpose, guest-link semantics, status and known gaps. [planning/next-epics.md](planning/next-epics.md) is the only outcome checklist.

## Start and recover

1. Read this file.
2. Read the README [Guest links](README.md#guest-links) and [Status](README.md#status) sections, then [docs/SECURITY.md](docs/SECURITY.md) before any change to publishing, sharing, the viewers, the console or the management transport.
3. Read [ARCHITECTURE.md](ARCHITECTURE.md) before a structural change and [DESIGN.md](DESIGN.md) before a visual one.
4. Read the selected outcome in `planning/next-epics.md`. Work on one selected outcome at a time.

After compaction or context loss, re-read this file, the guest-link rules, the security model and the selected outcome before the next consequential edit.

## Revision state

This is public `main`, the 0.1.0 preview in [CHANGELOG.md](CHANGELOG.md). It has the service, the optional standalone console, the guest-only bridge and the Hermes plugin with its packaged operating skill. Share tokens are 128 bits. There is no published image, package or release. Which revision a running service uses is not recorded here.

## Routes

| Task | Read |
|---|---|
| Service routes, guards, guest viewer | `app/main.py`, [docs/api.md](docs/api.md) |
| Library, shares, expiry and revocation | `app/store.py` (`Store`), `app/domain.py` |
| Rendering policy and CSP | `app/render.py`, `app/viewer/` |
| Standalone console and its login | `app/console.py`, `app/console/`, `app/hash_password.py`, [docs/configuration.md](docs/configuration.md) |
| One-command local launch | `app/local.py`, [docs/local.md](docs/local.md) |
| Agent tools and operating skill | `plugin/tools.py`, `plugin/client.py`, `plugin/skills/operating/SKILL.md`, [docs/hermes.md](docs/hermes.md) |
| Dashboard tab and panel | `plugin/dashboard/plugin_api.py`, `web/src/` (built into `app/console/` and `plugin/dashboard/dist/`) |
| Deployment, guest-only bridge, backup | `deploy/`, [docs/deploy.md](docs/deploy.md) |
| Checks and integration gates | [CONTRIBUTING.md](CONTRIBUTING.md), `scripts/verify.py`, `.claude/skills/verify-hermes-artifacts/SKILL.md` |

## Boundaries that must hold

- Publishing is private. It never creates a link.
- Sharing is a separate, explicit action. One link grants one immutable artifact, expires, and is revoked on its own. Only the token's SHA-256 is stored.
- Expired, revoked, unknown and malformed links return the same 404 page.
- Artifact content is untrusted. It runs in an opaque-origin sandboxed frame with a strict CSP and never enters the Hermes DOM.
- The machine credential stays server-side. The console is off unless configured, and the guest-only bridge never forwards `/internal/`.
- Revocation cannot recall downloaded copies, and CSP is not a network air gap. Keep these limits stated in [docs/SECURITY.md](docs/SECURITY.md#residual-limitations).

## Authority

Owner approval is required to change a boundary above, the security model, product scope, or a checklist priority. A checklist item is not permission to implement it. Push, release, deployment, installs into a live Hermes and agent smokes that spend credentials need explicit authorization.

## Done means

An outcome is complete only when its user-visible completion condition is verified on the real artifact. An implementation-only change does not tick an item. Passing tests or documentation checks are not runtime or user acceptance. Update `planning/next-epics.md` only for a verified outcome or an owner scope decision.

## Checks

[CONTRIBUTING.md](CONTRIBUTING.md#checks-every-change-should-pass) lists the checks every change should pass, starting with `uv run --frozen pytest tests/service`. The [integration gates](CONTRIBUTING.md#integration-gates) drive the real app. Record only gates that actually ran.
