import * as React from "react";
import type { Mode, Theme } from "../protocol";
import { NAVIGATION_NOTICE } from "../protocol";
import {
  ApiError, Artifact, Detail, Grant, KIND_LABEL, KINDS, Page, Share,
  formatBytes, formatDate, frameSource, json, request,
} from "./api";
import type { FrameSource, PanelHost } from "./host";
import { Badge, Button, Input, Select, timeAgo, useToast } from "./ui";

const { useState, useEffect, useCallback, useRef, useLayoutEffect } = React;
const SelectOption = ({ value, children }: { value: string; children: React.ReactNode }) => <option value={value}>{children}</option>;

const HostContext = React.createContext<PanelHost>(null as unknown as PanelHost);
const useHost = () => React.useContext(HostContext);

function useTheme(): Theme {
  const host = useHost();
  const [theme, setTheme] = useState<Theme>(host.theme);
  useEffect(() => host.onTheme(setTheme), [host]);
  return theme;
}

function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  const host = useHost();
  const hint = error.code === "not_configured"
    ? "Set HERMES_ARTIFACTS_URL and HERMES_ARTIFACTS_TOKEN for the dashboard process, then restart it."
    : error.code === "service_unreachable" ? "Start the artifact service or check its URL."
    : error.status === 401 && host.kind === "standalone" ? "Reload the page and sign in with the console credentials." : null;
  return (
    <div className="ha-state ha-state--error" role="alert">
      <p className="ha-state__title">{error.message}</p>
      {hint && <p className="ha-state__hint">{hint}</p>}
      {onRetry && <Button size="sm" outlined onClick={onRetry}>Try again</Button>}
    </div>
  );
}

function Library() {
  const host = useHost();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [kind, setKind] = useState("");
  const [profile, setProfile] = useState("");
  const [profiles, setProfiles] = useState<string[]>([]);
  const [items, setItems] = useState<Artifact[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    json<{ profiles: string[] }>("/profiles").then((r) => setProfiles(r.profiles), () => setProfiles([]));
  }, [attempt]);

  const load = useCallback(async (after: string | null) => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ limit: "30" });
    if (debounced) params.set("q", debounced);
    if (kind) params.set("kind", kind);
    if (profile) params.set("profile", profile);
    if (after) params.set("cursor", after);
    try {
      const page = await json<Page>(`/artifacts?${params}`);
      setItems((previous) => (after ? [...previous, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setLoading(false);
    }
  }, [debounced, kind, profile]);

  useEffect(() => { load(null); }, [load, attempt]);

  const filtered = Boolean(debounced || kind || profile);
  return (
    <div className="ha-library">
      <div className="ha-filters" role="search">
        <label className="ha-field ha-field--grow">
          <span>Search</span>
          <Input type="search" placeholder="Filter by title…" value={query} aria-label="Search artifacts"
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)} />
        </label>
        <label className="ha-field">
          <span>Format</span>
          <Select value={kind} onValueChange={setKind} aria-label="Format">
            <SelectOption value="">All formats</SelectOption>
            {KINDS.map((k) => <SelectOption key={k} value={k}>{KIND_LABEL[k]}</SelectOption>)}
          </Select>
        </label>
        <label className="ha-field">
          <span>Source</span>
          <Select value={profile} onValueChange={setProfile} aria-label="Source profile">
            <SelectOption value="">All sources</SelectOption>
            {profiles.map((p) => <SelectOption key={p} value={p}>{p}</SelectOption>)}
          </Select>
        </label>
        <Button size="sm" outlined onClick={() => setAttempt((n) => n + 1)}>Refresh</Button>
      </div>

      {error && <ErrorState error={error} onRetry={() => setAttempt((n) => n + 1)} />}

      {!error && !loading && items.length === 0 && (
        <div className="ha-state" data-testid="empty-state">
          <p className="ha-state__title">{filtered ? "No artifacts match these filters." : "No artifacts yet."}</p>
          <p className="ha-state__hint">
            {filtered
              ? "Clear the search or pick another format or source."
              : "Publish from any agent with the artifact_publish tool. Everything stays private until you share it."}
          </p>
        </div>
      )}

      {items.length > 0 && (
        <ul className="ha-list" aria-label="Artifacts" aria-busy={loading}>
          {items.map((artifact) => (
            <li key={artifact.id}>
              <button type="button" className="ha-row" onClick={() => host.navigate(artifact.slug)}>
                <span className="ha-row__title">{artifact.title}</span>
                <span className="ha-row__meta">
                  <Badge tone="outline">{KIND_LABEL[artifact.kind]}</Badge>
                  <span>{artifact.source_profile}</span>
                  <span>{formatBytes(artifact.byte_length)}</span>
                  <time dateTime={new Date(artifact.created_at * 1000).toISOString()}>{timeAgo(artifact.created_at)}</time>
                  {(artifact.active_shares ?? 0) > 0 && <Badge tone="warning">Shared ×{artifact.active_shares}</Badge>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {loading && <div className="ha-state ha-state--loading" aria-live="polite">Loading artifacts…</div>}
      {!loading && cursor && (
        <div className="ha-more"><Button size="sm" outlined onClick={() => load(cursor)}>Load more</Button></div>
      )}
    </div>
  );
}

/**
 * A native modal dialog joins the top layer above the fullscreen or full-window viewer that opened it.
 * The SDK dialogs portal to <body>, which the top layer hides.
 */
function Modal({ open, onClose, title, description, className, children }: {
  open: boolean; onClose: () => void; title: string; description: string; className?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = React.useId();
  useLayoutEffect(() => {
    const dialog = ref.current;
    if (open && !dialog?.open) dialog?.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className={`ha-dialog ${className ?? ""}`} aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-description`}
      onCancel={(event) => { if (event.target !== event.currentTarget) return; event.preventDefault(); onClose(); }}
      onClose={(event) => { if (event.target === event.currentTarget && open) onClose(); }}>
      <div className="ha-dialog__header">
        <h2 id={`${id}-title`} className="ha-dialog__title">{title}</h2>
        <p id={`${id}-description`} className="ha-dialog__description">{description}</p>
        <Button size="xs" ghost className="ha-dialog__close" aria-label="Close" onClick={onClose}>✕</Button>
      </div>
      {children}
    </dialog>
  );
}

function ViewerToast({ toast }: { toast: { message: string; type: "error" | "success" } | null }) {
  return (
    <div className="ha-toast" role="status" aria-live="polite">
      {toast && <p className={`ha-toast__message ha-toast__message--${toast.type}`}>{toast.message}</p>}
    </div>
  );
}

const EXPIRY_CHOICES: Array<[string, number]> = [
  ["1 hour", 3600], ["1 day", 86_400], ["7 days", 604_800], ["30 days", 2_592_000],
];

function ShareDialog({ artifact, shares, open, onClose, onChanged }: {
  artifact: Artifact; shares: Share[]; open: boolean; onClose: () => void; onChanged: () => void;
}) {
  const [expiry, setExpiry] = useState("604800");
  const [grant, setGrant] = useState<Grant | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<Share | null>(null);

  useEffect(() => { if (!open) { setGrant(null); setError(null); setStatus(null); } }, [open]);

  async function create() {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const created = await json<Grant>(`/artifacts/${encodeURIComponent(artifact.id)}/shares`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expires_in_seconds: Number(expiry) }),
      });
      setGrant(created);
      onChanged();
      setStatus("Share link created.");
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!grant) return;
    try {
      await navigator.clipboard.writeText(grant.url);
      setStatus("Link copied to the clipboard.");
    } catch {
      setError("Copy failed. Select the link and copy it manually.");
    }
  }

  async function revoke() {
    if (!revoking) return;
    setBusy(true);
    try {
      await request(`/shares/${encodeURIComponent(revoking.id)}`, { method: "DELETE" });
      setStatus("Share link revoked.");
      if (grant?.share.id === revoking.id) setGrant(null);
      onChanged();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  }

  return (
    <Modal open={open} onClose={onClose} className="ha-share" title={`Share “${artifact.title}”`}
      description={"A link grants view access to this one artifact. Anyone holding it can open it until it expires"
        + " or you revoke it. Revoking stops future access; it cannot recall copies already downloaded."}>
        <div className="ha-share__body">
        <div className="ha-share__create">
          <label className="ha-field">
            <span>Expires after</span>
            <Select value={expiry} onValueChange={setExpiry}>
              {EXPIRY_CHOICES.map(([label, seconds]) => <SelectOption key={seconds} value={String(seconds)}>{label}</SelectOption>)}
            </Select>
          </label>
          <Button onClick={create} disabled={busy}>{busy ? "Creating…" : "Create link"}</Button>
        </div>
        {error && <p className="ha-share__error" role="alert">{error}</p>}
        <p className="ha-share__status" role="status">{status}</p>
        {grant && (
          <div className="ha-share__grant" data-testid="share-grant">
            <label className="ha-field ha-field--grow">
              <span>Link (shown once; expires {formatDate(grant.share.expires_at)})</span>
              <Input readOnly value={grant.url} aria-label="Share link"
                onFocus={(e: React.FocusEvent<HTMLInputElement>) => e.target.select()} />
            </label>
            <Button outlined onClick={copy}>Copy</Button>
          </div>
        )}
        <div className="ha-share__list">
          <p className="ha-share__heading">Links for this artifact</p>
          {shares.length === 0 ? <p className="ha-muted">No links yet. This artifact is private.</p> : (
            <ul>
              {shares.map((s) => (
                <li key={s.id} className="ha-share__item">
                  <Badge tone={s.status === "active" ? "success" : "secondary"}>{s.status}</Badge>
                  <span className="ha-muted">
                    {s.status === "revoked" && s.revoked_at ? `revoked ${formatDate(s.revoked_at)}` : `expires ${formatDate(s.expires_at)}`}
                  </span>
                  {s.status === "active" && (
                    <Button size="xs" destructive outlined className="ha-share__revoke" onClick={() => setRevoking(s)}>Revoke</Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        </div>
        <Modal open={revoking !== null} onClose={() => setRevoking(null)} className="ha-confirm" title="Revoke this link?"
          description="People holding this link lose access immediately. Other links keep working.">
          <div className="ha-confirm__actions">
            <Button outlined onClick={() => setRevoking(null)} disabled={busy}>Cancel</Button>
            <Button destructive onClick={revoke} disabled={busy} autoFocus>{busy ? "Revoking…" : "Revoke"}</Button>
          </div>
        </Modal>
    </Modal>
  );
}

function Viewer({ slug }: { slug: string }) {
  const host = useHost();
  const theme = useTheme();
  const { toast, showToast } = useToast(3500);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [source, setSource] = useState<FrameSource | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [mode, setMode] = useState<Mode>("inline");
  const [sharing, setSharing] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const id = detail?.artifact.id;
  const path = `/artifacts/${encodeURIComponent(id ?? "")}`;

  const refresh = useCallback(() => {
    json<Detail>(`/slugs/${encodeURIComponent(slug)}`).then(setDetail, (err) => setError(err as ApiError));
  }, [slug]);

  useEffect(() => { setDetail(null); setError(null); refresh(); }, [refresh]);

  useEffect(() => {
    if (!id) return;
    setSource(null);
    frameSource(id, theme).then(setSource, (err) => setError(err as ApiError));
  }, [id, theme]);

  useEffect(() => { host.setMode(mode); }, [host, mode]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source === frame.current?.contentWindow && event.data === NAVIGATION_NOTICE) {
        showToast("The artifact tried to navigate away. It was blocked and reloaded.", "error");
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [showToast]);

  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const fit = () => {
      if (mode !== "inline") { element.style.height = ""; return; }
      const top = element.getBoundingClientRect().top + window.scrollY;
      const height = `${Math.max(320, Math.floor(window.innerHeight - top - (host.kind === "hermes" ? 0 : 12)))}px`;
      if (element.style.height !== height) element.style.height = height;
    };
    fit();
    const observer = new ResizeObserver(fit);
    for (let node = element.parentElement; node; node = node.parentElement) observer.observe(node);
    window.addEventListener("resize", fit);
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); };
  }, [host, mode, detail, error]);

  useEffect(() => {
    const sync = () => setMode((current) => (document.fullscreenElement === root.current ? "fullscreen" : current === "fullscreen" ? "inline" : current));
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  useLayoutEffect(() => {
    const element = root.current;
    if (mode !== "expanded" || !element || !("showPopover" in element)) return;
    element.popover = "manual";
    element.showPopover();
    return () => {
      if (element.matches(":popover-open")) element.hidePopover();
      element.removeAttribute("popover");
    };
  }, [mode]);

  useEffect(() => {
    if (mode !== "expanded") return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !sharing) setMode("inline"); };
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", onKey); };
  }, [mode, sharing]);

  async function toggleFullscreen() {
    if (document.fullscreenElement) { await document.exitFullscreen(); return; }
    if (!document.fullscreenEnabled || !root.current?.requestFullscreen) {
      setMode("expanded");
      showToast("Fullscreen is unavailable; using the full window instead.", "success");
      return;
    }
    try {
      await root.current.requestFullscreen();
    } catch {
      setMode("expanded");
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(host.privateLink(slug));
      showToast(host.kind === "hermes" ? "Private link copied. It opens only for signed-in board users."
        : "Private link copied. It opens only after the console sign-in.", "success");
    } catch {
      showToast("Copy failed. Copy the address bar instead.", "error");
    }
  }

  async function download() {
    if (!detail) return;
    try {
      const response = await request(`${path}/download`);
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const name = disposition.match(/filename="([^"]+)"/)?.[1] ?? "artifact";
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      showToast((err as ApiError).message, "error");
    }
  }

  const artifact = detail?.artifact;
  return (
    <div ref={root} className={`ha-viewer ha-viewer--${mode}`} data-testid="viewer" data-mode={mode}>
      <div className="ha-toolbar" role="toolbar" aria-label="Artifact actions">
        {mode === "inline"
          ? <Button size="sm" ghost onClick={() => host.navigate(null)} aria-label="Back to library">
              <span className="ha-wide">← Library</span><span className="ha-narrow" aria-hidden="true">←</span>
            </Button>
          : <Button size="sm" ghost onClick={() => (mode === "fullscreen" ? document.exitFullscreen() : setMode("inline"))}
              aria-label={`Exit ${mode === "fullscreen" ? "fullscreen" : "full window"}`}>
              <span className="ha-wide">✕ Exit {mode === "fullscreen" ? "fullscreen" : "full window"}</span>
              <span className="ha-narrow" aria-hidden="true">✕</span>
            </Button>}
        <div className="ha-toolbar__title">
          <span className="ha-toolbar__name" title={artifact?.title}>{artifact?.title ?? "Loading…"}</span>
          {artifact && (
            <span className="ha-toolbar__meta">
              {KIND_LABEL[artifact.kind]} · {formatBytes(artifact.byte_length)} · {artifact.source_profile} · {formatDate(artifact.created_at)}
              {(artifact.active_shares ?? 0) > 0 ? ` · shared ×${artifact.active_shares}` : " · private"}
            </span>
          )}
        </div>
        <div className="ha-toolbar__actions">
          <Button size="sm" onClick={() => setSharing(true)} disabled={!artifact}>Share</Button>
          {mode === "inline" && <Button size="sm" outlined className="ha-wide" onClick={copyLink} disabled={!artifact}>Copy link</Button>}
          <Button size="sm" outlined onClick={download} disabled={!artifact} aria-label="Download" title="Download">
            <span className="ha-wide">Download</span><span className="ha-narrow" aria-hidden="true">↓</span>
          </Button>
          {mode === "inline" && <Button size="sm" outlined className="ha-wide" onClick={() => setMode("expanded")}>Full window</Button>}
          {mode !== "fullscreen" && (
            <Button size="sm" outlined onClick={toggleFullscreen} aria-label="Fullscreen" title="Fullscreen">
              <span className="ha-wide">Fullscreen</span><span className="ha-narrow" aria-hidden="true">⤢</span>
            </Button>
          )}
        </div>
      </div>
      <div className="ha-stage">
        {error ? <ErrorState error={error} onRetry={() => { setError(null); refresh(); }} />
          : source === null ? <div className="ha-state ha-state--loading" aria-live="polite">Loading preview…</div>
          : <iframe ref={frame} className="ha-frame" title={artifact?.title ?? "Artifact preview"} sandbox="allow-scripts"
              referrerPolicy="no-referrer" data-testid="artifact-frame"
              {...("src" in source ? { src: source.src } : { srcDoc: source.srcdoc })} />}
      </div>
      {artifact && detail && (
        <ShareDialog artifact={artifact} shares={detail.shares} open={sharing} onClose={() => setSharing(false)}
          onChanged={refresh} />
      )}
      <ViewerToast toast={toast} />
    </div>
  );
}

export function Panel({ host }: { host: PanelHost }) {
  const [selected, setSelected] = useState<string | null>(host.route);
  useEffect(() => host.onRoute(setSelected), [host]);
  return (
    <HostContext.Provider value={host}>
      <div className={`ha-root ha-root--${host.kind}`}>
        {host.kind === "standalone" && (
          <header className="ha-masthead">
            <a className="ha-masthead__name" href="/console/" onClick={(e) => { e.preventDefault(); host.navigate(null); }}>Hermes Artifacts</a>
            <span className="ha-masthead__note">Private library · links are shared only on request</span>
          </header>
        )}
        {selected ? <Viewer key={selected} slug={selected} /> : <Library />}
      </div>
    </HostContext.Provider>
  );
}
