# Panel inside Hermes

Promise: the dashboard tab `/artifacts/*` is a thin host. It loads the same panel bytes from `dist/console/console.html` in a same-origin iframe and answers the panel's allowlisted requests with the dashboard's `authedFetch`. No credential crosses into the panel.

Code: `web/src/host/hermes.tsx` (adapter, allowlist, URL sync, Full window), `web/src/panel/host.ts` (`hermesHost`), `web/src/protocol.ts` (messages), `plugin/dashboard/plugin_api.py` (forwards to the service).

Drive: open `<dashboard>/artifacts`, take `[data-testid=artifacts-panel]`'s `content_frame()`, then use the panel selectors. `/artifacts/<slug>` opens that artifact; the outer URL follows the panel.
