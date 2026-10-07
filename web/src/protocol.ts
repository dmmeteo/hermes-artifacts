/** Messages between the Hermes host adapter and the shared panel. They carry data, never credentials. */
export const CHANNEL = "hermes-artifacts";
export const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const NAVIGATION_NOTICE = "hermes-artifacts:navigation-blocked";

export type Theme = "light" | "dark";
export type Mode = "inline" | "expanded" | "fullscreen";

export type PanelMessage =
  | { channel: typeof CHANNEL; type: "ready" }
  | { channel: typeof CHANNEL; type: "request"; id: number; method: string; path: string; contentType?: string; body?: string }
  | { channel: typeof CHANNEL; type: "navigate"; slug: string | null }
  | { channel: typeof CHANNEL; type: "mode"; mode: Mode };

export type HostMessage =
  | { channel: typeof CHANNEL; type: "init"; slug: string | null; theme: Theme; tokens: Record<string, string>; linkBase: string }
  | { channel: typeof CHANNEL; type: "response"; id: number; status: number; headers: Record<string, string>; body: ArrayBuffer }
  | { channel: typeof CHANNEL; type: "route"; slug: string | null }
  | { channel: typeof CHANNEL; type: "theme"; theme: Theme; tokens: Record<string, string> };

/** Host design tokens the panel adopts when embedded, so it wears the dashboard's theme. */
export const TOKENS = [
  "--color-background", "--color-foreground", "--color-card", "--color-popover", "--color-border",
  "--color-input", "--color-muted-foreground", "--color-primary", "--color-primary-foreground", "--color-ring",
  "--color-destructive", "--color-success", "--color-warning", "--radius",
];
