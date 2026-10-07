"""Hermes Artifacts plugin: agent tools, the packaged operating skill and a dashboard host for the panel."""

from pathlib import Path

from .tools import TOOLS, service_available

TOOLSET = "artifacts"
SKILL = Path(__file__).resolve().parent / "skills" / "operating" / "SKILL.md"
SKILL_DESCRIPTION = "How to publish, find, download, share and revoke Hermes Artifacts safely."


def register(ctx):
    for name, handler, emoji, schema in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=service_available, emoji=emoji, description=schema["description"])
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is not None:
        register_skill("operating", SKILL, description=SKILL_DESCRIPTION)
