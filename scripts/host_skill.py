"""Real host check: a fresh HERMES_HOME discovers the packaged skill and reaches the one shared library.

Run with the Hermes venv Python, HERMES_HOME set to an isolated home with the plugin installed, and
HERMES_PROFILE_NAME naming the profile to attribute. Publishes one note, lists the library and prints one
JSON report on stdout. Usage: python host_skill.py TITLE
"""

import json
import os
import sys
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402

load_hermes_dotenv(load_external_secrets=False)

from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402

discover_plugins()
import model_tools  # noqa: E402
from tools.skills_tool import skill_view, skills_list  # noqa: E402

listed = json.loads(skills_list())
names = [skill.get("name") for skill in listed.get("skills", [])]
viewed = json.loads(skill_view("artifacts:operating"))
offered = [d["function"]["name"] for d in model_tools.get_tool_definitions(quiet_mode=True)]


def call(name, args):
    return json.loads(model_tools.handle_function_call(name, args, task_id="skill-check", session_id="skill-check"))


# Plugin tools are deferred behind Tool Search; a model reads their descriptions through tool_describe.
described = json.dumps(call("tool_describe", {"names": ["artifact_publish", "artifact_share"]}) if "tool_describe" in offered
                       else [d for d in model_tools.get_tool_definitions(quiet_mode=True)])
published = call("artifact_publish", {"content": f"# {sys.argv[1]}\n", "kind": "markdown", "title": sys.argv[1]})
library = call("artifact_list", {"limit": 100})
home = Path(os.environ["HERMES_HOME"])
print(json.dumps({
    "profile": os.environ.get("HERMES_PROFILE_NAME"),
    "plugin_skills": get_plugin_manager().list_plugin_skills("artifacts"),
    "skills_list_has_operating": "artifacts:operating" in names,
    "skills_list_entry": next((s for s in listed.get("skills", []) if s.get("name") == "artifacts:operating"), None),
    "skill_view_success": viewed.get("success"),
    "skill_view_name": viewed.get("name"),
    "skill_view_mentions": [t for t in ("artifact_share", "artifact_revoke_share", "next_cursor", "source_profile",
                                        "not_configured", "immutable") if t in json.dumps(viewed)],
    "skill_not_copied_into_home": not any(p.name == "operating" for p in (home / "skills").glob("**/operating")),
    "tool_pointer": described.count("skill_view('artifacts:operating')") >= 2,
    "published_profile": published.get("artifact", {}).get("source_profile"),
    "library_titles": sorted(a["title"] for a in library.get("items", [])),
}))
