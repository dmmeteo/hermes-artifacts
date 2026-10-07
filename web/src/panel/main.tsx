import * as React from "react";
import { createRoot } from "react-dom/client";
import { useHostForApi } from "./api";
import { Panel } from "./app";
import { hermesHost, PanelHost, standaloneHost } from "./host";

function mount(host: PanelHost) {
  useHostForApi(host);
  document.documentElement.dataset.host = host.kind;
  createRoot(document.getElementById("root")!).render(<Panel host={host} />);
}

// The standalone page refuses framing (frame-ancestors 'none'), so a framed panel is the Hermes-hosted one.
if (window.parent !== window) {
  hermesHost().then(mount);
} else if (window.location.pathname === "/console" || window.location.pathname.startsWith("/console/")) {
  mount(standaloneHost());
} else {
  document.getElementById("root")!.textContent = "Open Hermes Artifacts from the Hermes dashboard or its standalone console.";
}
