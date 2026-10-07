# Hermes Artifacts

The implemented design. The standalone console (`/console/`, optional) serves the same panel as the dashboard tab and calls the service directly with its own login.

```mermaid
flowchart TD
    H["Hermes Dashboard
/artifacts page
host React SDK"] --> P["Hermes plugin
FastAPI adapter
6 agent tools
session auth"]
    A["Hermes agents
publish, list,
share, revoke"] --> P
    G["Guest browser
explicit share link"] --> V["guest host / token
reverse proxy, guest paths only
no access log
read-only viewer"]
    P -- "bearer machine credential
/internal/v1" --> S["Artifact service
Python 3.14 + FastAPI"]
    V --> S
    S --> D["SQLite
immutable bytes
metadata
expiring shares"]
```

The trusted viewer owns toolbar, full-window and fullscreen controls. Generated HTML runs in an opaque-origin sandboxed frame behind a CSP; in the dashboard it sits inside a trusted wrapper frame whose CSP blocks the artifact's own navigations. The public host has no library or management UI.
