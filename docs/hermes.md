# Hermes integration

The plugin in `plugin/` connects [Hermes](https://hermes-agent.nousresearch.com/docs) to a running Artifacts service. It adds agent tools, a packaged skill and an **Artifacts** dashboard tab. It stores nothing itself: the service owns the library, and the plugin forwards to the machine API with a credential held server-side.

Tested with Hermes 0.21.x on its Python 3.11 runtime. Hermes's plugin contract is documented in [Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/) and [Extending the Dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard).

## Install

1. Run the service ([README quickstart](../README.md#quickstart) or [deploy.md](deploy.md)).
2. Copy `plugin/` to `<HERMES_HOME>/plugins/artifacts/`. The copy already contains the built dashboard bundle in `plugin/dashboard/dist/`.
3. Add `artifacts` to `plugins.enabled` in `<HERMES_HOME>/config.yaml`. Hermes imports a user plugin's dashboard backend only when it is enabled.
4. Add the service URL and machine credential to `<HERMES_HOME>/.env` and keep the file mode 0600:

   ```sh
   HERMES_ARTIFACTS_URL=http://127.0.0.1:8080
   HERMES_ARTIFACTS_TOKEN=<the service's ARTIFACTS_API_TOKEN>
   ```

5. Restart the dashboard; plugin backend routes mount at startup. New agent sessions pick up the tools and the skill.

Several profiles can point at the same service. They all see one library; `source_profile` records which profile published each artifact.

The plugin's install name is `artifacts`, matching its manifest and `artifacts:operating` skill namespace.
It conflicts with the catalog's `artifact-relay` plugin because both register `artifact_publish`. Enable only one of them.
Both `HERMES_ARTIFACTS_URL` and `HERMES_ARTIFACTS_TOKEN` are required. The client resolves them through Hermes's active secret scope.

Uninstall: remove `artifacts` from `plugins.enabled`, delete the plugin directory and restart the dashboard. The service and its data are unaffected.

## Agent tools

All tools are in the `artifacts` toolset. Hermes defers plugin tools behind Tool Search by default, so a model reaches them through `tool_search`, `tool_describe` and `tool_call`.

| Tool | Does |
|---|---|
| `artifact_publish` | Save a file (`path`) or inline text (`content` + `kind`) privately. The kind is inferred from `.html .htm .md .markdown .svg .png .webp`. Returns the artifact and its dashboard path. Creates no link. |
| `artifact_list` | Newest first, metadata only. Filters `query` (title), `kind`, `profile`; `limit` up to 100 with `cursor` pagination. |
| `artifact_get` | Metadata, all shares with status, and a text preview for HTML, Markdown and SVG. |
| `artifact_download` | Write the original bytes atomically to a resolved local path (`overwrite` to replace an existing file). Hermes write denials and approval requirements still apply. |
| `artifact_share` | Create one guest link. `expires_in_hours` defaults to 168, maximum 8760. The URL is returned once. |
| `artifact_revoke_share` | Revoke one link by `share.id`. Other links to the same artifact keep working. |

The plugin also registers the read-only skill `artifacts:operating` (`plugin/skills/operating/SKILL.md`). `skills_list` shows it and `skill_view('artifacts:operating')` loads it. It is not copied into `<HERMES_HOME>/skills`.

Publishing by path checks Hermes's read policy on the resolved file before reading it. The resolved file must have one of the supported extensions above. An explicit `kind` must match that extension.
Downloads check Hermes's write policy on the resolved destination, including symlink targets. The tool refuses paths that need write approval because it has no approval channel. `overwrite=true` only permits replacing an ordinary existing file.
These checks follow the host's policy, which is defense in depth rather than filesystem isolation. They do not confine all access to the working directory. Other local processes can still change files or directories while a tool runs.

Sharing only when the user asks is enforced by tool wording and by sharing being a separate call, not by policy. Every share is visible and revocable in the dashboard.

## Dashboard tab

The manifest registers `/artifacts/*`: the library is `/artifacts` and one artifact is `/artifacts/<slug>`. A slug names an artifact but grants nothing; it needs the Hermes session.

The tab hosts the same panel the standalone console serves, in an iframe. The panel asks the tab, via `postMessage`, to make one of a fixed set of API requests with the Hermes session (`authedFetch` to `/api/plugins/artifacts/...`). The machine credential never reaches the browser. Artifact previews run in sandboxed, opaque-origin frames and never enter the dashboard's DOM.

## Without Hermes

Use the [standalone console](configuration.md#standalone-console) for a UI and the [machine API](api.md) for agents. Both work on the same library as the plugin.
