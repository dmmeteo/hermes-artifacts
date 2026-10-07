"""One bounded Hermes agent turn inside an isolated HERMES_HOME (run with the Hermes venv).

Reads {"api_key", "base_url", "model"} from stdin so the credential never touches
argv, the environment, disk or logs. Prints a JSON report with the tool calls the
model made (credential-free) and the final response.
Usage: python agent_runner.py WORK_DIR MAX_TURNS < credentials.json
"""

import json
import sys
from pathlib import Path

work, max_turns = Path(sys.argv[1]), int(sys.argv[2])
credentials = json.loads(sys.stdin.readline())

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402

load_hermes_dotenv(load_external_secrets=False)
from hermes_cli.plugins import discover_plugins  # noqa: E402

discover_plugins()
from run_agent import AIAgent  # noqa: E402

PROMPT = f"""You are running an end-to-end check of the Hermes Artifacts tools.
1. Write a small self-contained interactive HTML page to {work}/tip-calculator.html: a tip calculator with
   inline CSS and inline JavaScript only (no external URLs, fonts or scripts).
2. Write a short Markdown note to {work}/tip-calculator.md describing the page in 3-5 lines.
3. Publish both files privately with the artifact_publish tool (titles "Tip calculator" and "Tip calculator notes").
4. Create a guest share link ONLY for the HTML artifact with artifact_share, expiring in 24 hours.
5. Reply with both artifact ids and the share URL.
The artifact tools may be behind tool_search; find them there if they are not listed directly."""

agent = AIAgent(
    model=credentials["model"], provider="openai-codex", api_mode="codex_responses",
    api_key=credentials["api_key"], base_url=credentials["base_url"],
    max_iterations=max_turns, quiet_mode=True, session_id="artifacts-agent-smoke",
)
credentials.clear()
result = agent.run_conversation(PROMPT)

calls = []
for message in result.get("messages", []):
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {"raw": str(function.get("arguments"))[:200]}
        if isinstance(arguments.get("content"), str):
            arguments["content"] = f"<{len(arguments['content'])} chars>"
        calls.append({"name": function.get("name"), "arguments": arguments})
tool_results = [
    {"tool_call_id": m.get("tool_call_id"), "content": str(m.get("content"))[:600]}
    for m in result.get("messages", []) if m.get("role") == "tool"
]
print(json.dumps({
    "completed": result.get("completed"), "failed": result.get("failed"), "error": result.get("error"),
    "api_calls": result.get("api_calls"), "tool_calls": calls, "tool_results": tool_results,
    "final_response": result.get("final_response"),
}))
