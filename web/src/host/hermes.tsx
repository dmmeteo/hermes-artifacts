import { React, SDK } from "../sdk";
import { CHANNEL, Mode, PanelMessage, SLUG_RE, Theme, TOKENS } from "../protocol";

/**
 * The Hermes dashboard tab: a thin, trusted host for the shared panel. It loads the panel's static
 * assets in a same-origin iframe, answers the panel's requests with the dashboard's own authenticated
 * fetch (credentials never cross into the panel), and keeps /artifacts/<slug> in step with the panel.
 */

const { useEffect, useLayoutEffect, useRef, useState } = React;
const BASE = "/api/plugins/artifacts";
const SCRIPT = (document.currentScript as HTMLScriptElement | null)?.src ?? `${window.location.origin}/dashboard-plugins/artifacts/dist/index.js`;
const PANEL_URL = new URL("console/console.html", SCRIPT).href;
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ALLOWED: Array<[string, RegExp]> = [
  ["GET", /^\/artifacts(\?[A-Za-z0-9_.~%=&+-]{0,600})?$/],
  ["GET", /^\/profiles$/],
  ["GET", /^\/slugs\/[a-z0-9-]{1,64}$/],
  ["GET", new RegExp(`^/artifacts/${UUID}$`)],
  ["GET", new RegExp(`^/artifacts/${UUID}/document\\?theme=(light|dark)$`)],
  ["GET", new RegExp(`^/artifacts/${UUID}/download$`)],
  ["POST", new RegExp(`^/artifacts/${UUID}/shares$`)],
  ["DELETE", new RegExp(`^/shares/${UUID}$`)],
];
const SLUG_PATH = /\/artifacts\/([a-z0-9]+(?:-[a-z0-9]+)*)\/?$/;

export function allowed(method: string, path: string): boolean {
  return ALLOWED.some(([m, pattern]) => m === method && pattern.test(path));
}

function selectedFromUrl(): string | null {
  return window.location.pathname.match(SLUG_PATH)?.[1] ?? null;
}

function libraryPath(): string {
  return window.location.pathname.replace(/\/artifacts(\/.*)?$/, "/artifacts");
}

function parseColor(value: string): [number, number, number, number] | null {
  const parts = value.match(/-?[\d.]+/g)?.map(Number);
  if (!parts || parts.length < 3) return null;
  const scale = value.trim().startsWith("color(") ? 255 : 1;
  return [parts[0] * scale, parts[1] * scale, parts[2] * scale, parts.length > 3 ? parts[3] : 1];
}

function hostTheme(from: Element | null): Theme {
  let node: Element | null = from ?? document.body;
  while (node) {
    const color = parseColor(getComputedStyle(node).backgroundColor);
    if (color && color[3] > 0.5) {
      const luminance = (0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]) / 255;
      return luminance > 0.5 ? "light" : "dark";
    }
    node = node.parentElement;
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function hostTokens(element: Element): Record<string, string> {
  const style = getComputedStyle(element);
  const tokens: Record<string, string> = { font: getComputedStyle(document.body).fontFamily };
  for (const name of TOKENS) {
    const value = style.getPropertyValue(name).trim();
    if (value) tokens[name] = value;
  }
  return tokens;
}

function ArtifactsHost() {
  const root = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [mode, setMode] = useState<Mode>("inline");

  useEffect(() => {
    const send = (message: object, transfer: Transferable[] = []) =>
      frame.current?.contentWindow?.postMessage({ channel: CHANNEL, ...message }, window.location.origin, transfer);
    const appearance = () => ({ theme: hostTheme(root.current), tokens: hostTokens(root.current ?? document.body) });

    async function answer(id: number, method: string, path: string, contentType?: string, body?: string) {
      let status = 502;
      let headers: Record<string, string> = { "content-type": "application/json" };
      let payload: ArrayBuffer = new TextEncoder().encode(JSON.stringify(
        { error: { code: "network", message: "The Hermes dashboard could not be reached." } })).buffer as ArrayBuffer;
      if (!allowed(method, path) || (body !== undefined && body.length > 4096)) {
        status = 400;
        payload = new TextEncoder().encode(JSON.stringify(
          { error: { code: "invalid_request", message: "This request is not part of the Artifacts panel." } })).buffer as ArrayBuffer;
      } else {
        try {
          const reply = await SDK.authedFetch(`${BASE}${path}`, {
            method, body, headers: contentType ? { "Content-Type": contentType } : undefined,
          });
          status = reply.status;
          headers = {};
          for (const name of ["content-type", "content-disposition"]) {
            const value = reply.headers.get(name);
            if (value) headers[name] = value;
          }
          payload = await reply.arrayBuffer();
        } catch {
          /* reported as the network error above */
        }
      }
      send({ type: "response", id, status, headers, body: payload }, [payload]);
    }

    const onMessage = (event: MessageEvent) => {
      const data = event.data as PanelMessage;
      if (event.source !== frame.current?.contentWindow || event.origin !== window.location.origin) return;
      if (data?.channel !== CHANNEL) return;
      if (data.type === "ready") {
        send({ type: "init", slug: selectedFromUrl(), linkBase: window.location.origin + libraryPath(), ...appearance() });
      } else if (data.type === "request" && Number.isInteger(data.id) && typeof data.path === "string") {
        answer(data.id, String(data.method), data.path, data.contentType, data.body);
      } else if (data.type === "navigate" && (data.slug === null || SLUG_RE.test(String(data.slug)))) {
        const url = new URL(window.location.href);
        url.pathname = data.slug ? `${libraryPath()}/${data.slug}` : libraryPath();
        window.history.pushState({ artifacts: data.slug }, "", url);
        window.dispatchEvent(new PopStateEvent("popstate", { state: { artifacts: data.slug } }));
      } else if (data.type === "mode" && ["inline", "expanded", "fullscreen"].includes(data.mode)) {
        setMode(data.mode);
      }
    };
    window.addEventListener("message", onMessage);

    // The host tab is the splat route /artifacts/*. Its sidebar link points at that literal path and
    // host navigations inside the route emit no event a plugin can observe, so follow the URL.
    let last = selectedFromUrl();
    const sync = () => {
      if (/\/artifacts\/(\*|%2A)$/.test(window.location.pathname)) {
        window.history.replaceState(window.history.state, "", libraryPath() + window.location.search);
        window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
        return;
      }
      const current = selectedFromUrl();
      if (current !== last) {
        last = current;
        send({ type: "route", slug: current });
      }
    };
    sync();
    window.addEventListener("popstate", sync);
    const timer = window.setInterval(sync, 300);
    const observer = new MutationObserver(() => send({ type: "theme", ...appearance() }));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class", "data-theme"] });
    observer.observe(document.body, { attributes: true, attributeFilter: ["style", "class"] });
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("popstate", sync);
      window.clearInterval(timer);
      observer.disconnect();
    };
  }, []);

  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const fit = () => {
      if (mode !== "inline") { element.style.height = ""; return; }
      const top = element.getBoundingClientRect().top + window.scrollY;
      const height = `${Math.max(320, Math.floor(window.innerHeight - top - 12))}px`;
      if (element.style.height !== height) element.style.height = height;
    };
    fit();
    const observer = new ResizeObserver(fit);
    for (let node = element.parentElement; node; node = node.parentElement) observer.observe(node);
    window.addEventListener("resize", fit);
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); };
  }, [mode]);

  useLayoutEffect(() => {
    // Full window joins the top layer as a manual popover so it covers the dashboard's sidebar and header.
    const element = root.current;
    if (mode !== "expanded" || !element || !("showPopover" in element)) return;
    element.popover = "manual";
    element.showPopover();
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      if (element.matches(":popover-open")) element.hidePopover();
      element.removeAttribute("popover");
      document.body.style.overflow = previous;
    };
  }, [mode]);

  return (
    <div ref={root} className={`ha-host ha-host--${mode}`} data-testid="artifacts-host">
      <iframe ref={frame} className="ha-host__frame" src={PANEL_URL} title="Hermes Artifacts"
        allow="fullscreen; clipboard-write" referrerPolicy="no-referrer" data-testid="artifacts-panel" />
    </div>
  );
}

window.__HERMES_PLUGINS__.register("artifacts", ArtifactsHost);
