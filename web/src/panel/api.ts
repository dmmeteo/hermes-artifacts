import type { Theme } from "../protocol";
import type { FrameSource, PanelHost } from "./host";

export type Kind = "html" | "markdown" | "svg" | "png" | "webp";
export const KINDS: Kind[] = ["html", "markdown", "svg", "png", "webp"];
export const KIND_LABEL: Record<Kind, string> = { html: "HTML", markdown: "Markdown", svg: "SVG", png: "PNG", webp: "WebP" };

export interface Artifact {
  id: string;
  slug: string;
  title: string;
  kind: Kind;
  created_at: number;
  source_profile: string;
  source_session_id: string | null;
  byte_length: number;
  content_sha256: string;
  active_shares?: number;
}

export interface Share {
  id: string;
  artifact_id: string;
  created_at: number;
  expires_at: number;
  revoked_at: number | null;
  status: "active" | "expired" | "revoked";
}

export interface Page { items: Artifact[]; next_cursor: string | null }
export interface Detail { artifact: Artifact; shares: Share[] }
export interface Grant { share: Share; url: string }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

let host: PanelHost;

export function useHostForApi(value: PanelHost): void {
  host = value;
}

async function failure(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `Request failed with HTTP ${response.status}.`;
  try {
    const body = await response.json();
    if (body?.error?.message) ({ code, message } = body.error);
    else if (typeof body?.detail === "string") message = body.detail;
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(response.status, code, message);
}

export async function request(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await host.request(path, init);
  } catch {
    throw new ApiError(0, "network", "The artifact library could not be reached.");
  }
  if (!response.ok) throw await failure(response);
  return response;
}

export async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return (await request(path, init)).json() as Promise<T>;
}

export async function frameSource(artifactId: string, theme: Theme): Promise<FrameSource> {
  try {
    return await host.frame(artifactId, theme);
  } catch (err) {
    throw err instanceof Response ? await failure(err) : new ApiError(0, "network", "The preview could not be loaded.");
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDate(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
