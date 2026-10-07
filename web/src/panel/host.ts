import { CHANNEL, HostMessage, Mode, SLUG_RE, Theme, TOKENS } from "../protocol";
import { ArtifactDocument, wrapperDocument } from "./frame";

export type FrameSource = { src: string } | { srcdoc: string };

/** Everything the panel needs from where it is hosted. The panel itself never reads host globals. */
export interface PanelHost {
  kind: "standalone" | "hermes";
  request(path: string, init?: RequestInit): Promise<Response>;
  frame(artifactId: string, theme: Theme): Promise<FrameSource>;
  route(): string | null;
  navigate(slug: string | null): void;
  onRoute(listener: (slug: string | null) => void): () => void;
  theme(): Theme;
  onTheme(listener: (theme: Theme) => void): () => void;
  privateLink(slug: string): string;
  setMode(mode: Mode): void;
}

const CONSOLE = "/console";

/** The service's own /console: Basic auth is the browser's, the API sits beside the page. */
export function standaloneHost(): PanelHost {
  const scheme = window.matchMedia("(prefers-color-scheme: dark)");
  const route = () => window.location.pathname.match(/^\/console\/([a-z0-9]+(?:-[a-z0-9]+)*)\/?$/)?.[1] ?? null;
  return {
    kind: "standalone",
    request: (path, init) => fetch(`${CONSOLE}/api${path}`, {
      ...init, credentials: "same-origin", cache: "no-store",
      headers: { ...(init?.headers as Record<string, string> | undefined), "X-Artifacts-Console": "1" },
    }),
    frame: async (id, theme) => ({ src: `${CONSOLE}/api/artifacts/${encodeURIComponent(id)}/frame?theme=${theme}` }),
    route,
    navigate(slug) {
      window.history.pushState({ artifacts: slug }, "", slug ? `${CONSOLE}/${slug}` : `${CONSOLE}/`);
      window.dispatchEvent(new PopStateEvent("popstate", { state: { artifacts: slug } }));
    },
    onRoute(listener) {
      const sync = () => listener(route());
      window.addEventListener("popstate", sync);
      return () => window.removeEventListener("popstate", sync);
    },
    theme: () => (scheme.matches ? "dark" : "light"),
    onTheme(listener) {
      const sync = () => listener(scheme.matches ? "dark" : "light");
      scheme.addEventListener("change", sync);
      return () => scheme.removeEventListener("change", sync);
    },
    privateLink: (slug) => `${window.location.origin}${CONSOLE}/${slug}`,
    setMode: () => undefined,
  };
}

/**
 * Inside the Hermes dashboard: a same-origin iframe whose requests travel to the trusted host adapter,
 * which makes them with the dashboard's own session. Only the adapter's window, at this origin, is heard.
 */
export function hermesHost(): Promise<PanelHost> {
  const parent = window.parent;
  const origin = window.location.origin;
  const post = (message: object) => parent.postMessage({ channel: CHANNEL, ...message }, origin);
  const pending = new Map<number, (message: Extract<HostMessage, { type: "response" }>) => void>();
  const routeListeners = new Set<(slug: string | null) => void>();
  const themeListeners = new Set<(theme: Theme) => void>();
  let next = 1;
  let slug: string | null = null;
  let theme: Theme = "dark";
  let linkBase = "";

  const applyTokens = (tokens: Record<string, string>) => {
    for (const name of TOKENS) {
      const value = tokens[name];
      if (typeof value === "string" && value.length <= 200) document.documentElement.style.setProperty(name, value);
    }
    if (typeof tokens.font === "string" && tokens.font.length <= 300) {
      document.documentElement.style.setProperty("--ha-font", tokens.font);
    }
  };

  return new Promise((resolve) => {
    window.addEventListener("message", (event: MessageEvent) => {
      const data = event.data as HostMessage;
      if (event.source !== parent || event.origin !== origin || data?.channel !== CHANNEL) return;
      if (data.type === "response") {
        pending.get(data.id)?.(data);
        pending.delete(data.id);
      } else if (data.type === "route") {
        slug = data.slug && SLUG_RE.test(data.slug) ? data.slug : null;
        routeListeners.forEach((listener) => listener(slug));
      } else if (data.type === "theme") {
        theme = data.theme === "light" ? "light" : "dark";
        applyTokens(data.tokens);
        themeListeners.forEach((listener) => listener(theme));
      } else if (data.type === "init" && !linkBase) {
        slug = data.slug && SLUG_RE.test(data.slug) ? data.slug : null;
        theme = data.theme === "light" ? "light" : "dark";
        linkBase = String(data.linkBase);
        applyTokens(data.tokens);
        resolve(host);
      }
    });

    const request = (path: string, init?: RequestInit) => new Promise<Response>((done) => {
      const id = next++;
      pending.set(id, (reply) => done(new Response(reply.status === 204 ? null : reply.body,
        { status: reply.status, headers: reply.headers })));
      const headers = new Headers(init?.headers);
      post({
        type: "request", id, method: init?.method ?? "GET", path,
        contentType: headers.get("Content-Type") ?? undefined,
        body: typeof init?.body === "string" ? init.body : undefined,
      });
    });

    const host: PanelHost = {
      kind: "hermes",
      request,
      async frame(id, frameTheme) {
        const reply = await request(`/artifacts/${encodeURIComponent(id)}/document?theme=${frameTheme}`);
        if (!reply.ok) throw reply;
        return { srcdoc: wrapperDocument(await reply.json() as ArtifactDocument) };
      },
      route: () => slug,
      navigate(target) {
        slug = target;
        post({ type: "navigate", slug: target });
        routeListeners.forEach((listener) => listener(slug));
      },
      onRoute(listener) { routeListeners.add(listener); return () => routeListeners.delete(listener); },
      theme: () => theme,
      onTheme(listener) { themeListeners.add(listener); return () => themeListeners.delete(listener); },
      privateLink: (target) => `${linkBase}/${target}`,
      setMode: (mode) => post({ type: "mode", mode }),
    };
    post({ type: "ready" });
  });
}
