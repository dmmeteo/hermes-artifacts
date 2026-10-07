# Shared library

Promise: one SQLite library chosen by the operator (`ARTIFACTS_DB`). No profile owns an artifact; `source_profile` is a label and a filter. The service and the standalone console run without Hermes.

Code: `app/store.py` (schema, unchanged), `app/main.py` (`/internal/v1`), `plugin/tools.py` (`_profile()` sets attribution).

Drive: publish with `source_profile=prime` and `steve` via `POST /internal/v1/artifacts`; `GET /internal/v1/artifacts` lists both; `host_skill.py` in another `HERMES_HOME` lists them too. Expect every row regardless of the caller's profile.
