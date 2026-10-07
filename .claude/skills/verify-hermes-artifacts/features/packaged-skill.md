# Packaged operating skill

Promise: enabling the plugin makes `artifacts:operating` available through `skills_list` and `skill_view`; the `artifact_publish` and `artifact_share` descriptions point to it. It is read from the plugin directory and is not in the system prompt index.

Code: `plugin/__init__.py` (`register_skill`), `plugin/skills/operating/SKILL.md`, `scripts/host_skill.py`.

Drive: `HERMES_HOME=<isolated home> HERMES_PROFILE_NAME=x $HERMES_CHECKOUT/venv/bin/python scripts/host_skill.py TITLE` from the Hermes checkout. Expect `skills_list_has_operating`, `skill_view_success`, `tool_pointer` all true.
