/**
 * Data-driven rendering for extension UI hooks (manifest-declared
 * `entrypoints.quick_button` and `entrypoints.page`). Replaces per-extension
 * hardcoded buttons (ASK toolbar button, project-structure sidebar icon) with
 * a single path driven by `/api/extensions/ui-hooks`.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { API } from "src/api";
import { eventBus } from "src/lib/eventBus";
import { scopedSnapshotKey, sharedSnapshotPoller } from "src/lib/sharedSnapshotPoller";
import { trackPromise } from "src/progress/store";
import Icon, { ICON_NAMES, type IconName } from "./Icon";
import { ExtensionModuleSlot, useExtensionAuthScope } from "./ExtensionSlots";

export interface HookAction {
  type: "navigate" | "ensure" | "module";
  path?: string;
  endpoint?: string;
  path_template?: string;
  id_field?: string;
  include_cwd?: boolean;
  module_url?: string;
}

export type QuickButtonPlacement = "session" | "settings";

export interface QuickButtonHook {
  extension_id: string;
  extension_name: string;
  label: string;
  icon?: string;
  action: HookAction;
  placements: QuickButtonPlacement[];
}

export interface PageHook {
  extension_id: string;
  extension_name: string;
  id: string;
  label: string;
  icon?: string;
  open: HookAction;
  badge?: { endpoint: string };
}

export interface UiHooks {
  quick_buttons: QuickButtonHook[];
  pages: PageHook[];
}

interface UiHooksPayload {
  hooks?: UiHooks;
}

export interface HookActionContext {
  navigate: (path: string) => void;
  cwd: string;
  openAsk?: () => void;
  askSessionPath?: string;
  /** Marks a freshly-created/ensured session id as routeable before the
   * sessions list catches up, preventing the route guard from bouncing the
   * navigation back to the default view. */
  markSessionKnown?: (id: string) => void;
}

function isKnownIcon(name: unknown): name is IconName {
  return typeof name === "string" && (ICON_NAMES as readonly string[]).includes(name);
}

function sessionPathForId(sessionId: string): string {
  return `/s/${encodeURIComponent(sessionId)}`;
}

function parseVirtualSingletonPath(path: string): { extensionId: string; slug: string } | null {
  const match = path.match(/^\/s\/([^/]+)\/?$/);
  if (!match) return null;
  let sessionId: string;
  try {
    sessionId = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const virtual = sessionId.match(/^virtual:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)$/);
  if (!virtual) return null;
  return { extensionId: virtual[1], slug: virtual[2] };
}

async function tryEnsureAssistantSingleton(path: string, ctx: HookActionContext): Promise<boolean> {
  const parsed = parseVirtualSingletonPath(path);
  if (!parsed || parsed.slug !== "assistant") return false;
  try {
    const endpoint = `/api/extensions/${encodeURIComponent(parsed.extensionId)}/backend/${encodeURIComponent(parsed.slug)}/ensure`;
    const res = await fetch(`${API}${endpoint}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = res.ok ? await res.json().catch(() => ({})) : {};
    if (!res.ok || data?.error) return false;
    const idValue = data?.id ?? data?.session_id;
    if (idValue == null || String(idValue) === "") return false;
    const sessionId = String(idValue);
    ctx.markSessionKnown?.(sessionId);
    ctx.navigate(sessionPathForId(sessionId));
    return true;
  } catch {
    return false;
  }
}

/** Execute a navigate/ensure action. `module` actions are rendered inline by
 *  the caller (ExtensionModuleSlot), never run imperatively. */
export async function runHookAction(action: HookAction, ctx: HookActionContext): Promise<void> {
  if (action.type === "navigate" && action.path) {
    // Opening the Ask singleton must go through openAsk so the navigation is
    // marked intentional; a bare navigate is bounced by the auto-select-first
    // effect that redirects the default Ask landing to a real session.
    // askSessionPath is URL-encoded (colons → %3A); manifests may declare the
    // raw form. Match against both so either spelling routes through openAsk.
    const askRaw = ctx.askSessionPath && decodeURIComponent(ctx.askSessionPath);
    if (
      ctx.openAsk &&
      ctx.askSessionPath &&
      (action.path === ctx.askSessionPath || action.path === askRaw)
    ) {
      ctx.openAsk();
      return;
    }
    // Back-compat for stale/dev-installed Assistant manifests that still
    // declare the old virtual route even though the backing UI is a real
    // ensured session. Ensure first and navigate to the returned session id;
    // if the endpoint is unavailable, fall back to the literal route.
    if (await tryEnsureAssistantSingleton(action.path, ctx)) return;
    ctx.navigate(action.path);
    return;
  }
  if (action.type === "ensure" && action.endpoint && action.path_template) {
    try {
      const res = await fetch(`${API}${action.endpoint}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action.include_cwd ? { cwd: ctx.cwd } : {}),
      });
      const data = res.ok ? await res.json().catch(() => ({})) : {};
      if (!res.ok || data.error) {
        window.alert(data?.detail || data?.error || `${action.endpoint} failed: ${res.status}`);
        return;
      }
      const idField = action.id_field || "session_id";
      const idValue = data[idField] != null ? String(data[idField]) : "";
      if (idValue) ctx.markSessionKnown?.(idValue);
      const path = action.path_template.replace(
        `{${idField}}`,
        idValue ? encodeURIComponent(idValue) : "",
      );
      ctx.navigate(path);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  }
}

export function useExtensionUiHooks(): UiHooks {
  const [hooks, setHooks] = useState<UiHooks>({ quick_buttons: [], pages: [] });

  const refresh = useCallback(async () => {
    const { promise } = trackPromise("extensions:ui-hooks", async () => {
      const res = await fetch(`${API}/api/extensions/ui-hooks`, { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()) as UiHooksPayload;
    });
    try {
      const payload = await promise;
      const h = payload.hooks || { quick_buttons: [], pages: [] };
      setHooks({ quick_buttons: h.quick_buttons || [], pages: h.pages || [] });
    } catch {
      setHooks({ quick_buttons: [], pages: [] });
    }
  }, []);

  useEffect(() => {
    void refresh();
    return eventBus.subscribe("extensions_changed", () => {
      void refresh();
    });
  }, [refresh]);

  return hooks;
}

const BADGE_POLL_MS = 120_000;

/** Polls each page's badge endpoint (GET → {count}) and returns the latest
 *  number keyed by `${extension_id}:${page_id}`. */
export function useExtensionPageBadges(pages: PageHook[]): Record<string, number> {
  const authScopeKey = useExtensionAuthScope();
  const [counts, setCounts] = useState<Record<string, number>>({});
  const keyed = useMemo(
    () => pages.filter((p) => p.badge?.endpoint).map((p) => ({ key: `${p.extension_id}:${p.id}`, endpoint: p.badge!.endpoint })),
    [pages],
  );

  useEffect(() => {
    if (!keyed.length) {
      setCounts({});
      return;
    }
    const pollers = keyed.map((item) => sharedSnapshotPoller(scopedSnapshotKey(API, authScopeKey, `extension-badge:${item.endpoint}`), {
      minIntervalMs: BADGE_POLL_MS,
      cadenceMs: BADGE_POLL_MS,
      load: async () => {
        const res = await fetch(`${API}${item.endpoint}`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const count = typeof data.count === "number" ? data.count : Number(data.count);
        return Number.isFinite(count) ? count : 0;
      },
    }));
    const unsubscribes = pollers.map((poller, index) => poller.subscribe((count) => {
      setCounts((current) => current[keyed[index].key] === count
        ? current
        : { ...current, [keyed[index].key]: count });
    }));
    const refresh = () => { for (const poller of pollers) void poller.request(); };
    const offChanged = eventBus.subscribe("extensions_changed", refresh);
    const offUpdates = eventBus.subscribe("project_updates_changed", refresh);
    return () => {
      for (const unsubscribe of unsubscribes) unsubscribe();
      offChanged();
      offUpdates();
    };
  }, [authScopeKey, keyed]);

  return counts;
}

function HookGlyph({ icon, label, size }: { icon?: string; label: string; size: number }) {
  if (isKnownIcon(icon)) return <Icon name={icon} size={size} />;
  return <span className="extension-hook-glyph">{label.slice(0, 1).toUpperCase()}</span>;
}

function quickButtonIconClass(icon?: string): string {
  if (!isKnownIcon(icon)) return "";
  return `extension-quick-button--icon-${icon}`;
}

interface QuickButtonProps {
  context: HookActionContext;
  className?: string;
  variant: "toolbar" | "topbar";
  /** Which mount surface this instance renders; buttons whose manifest
   * `placements` excludes it are skipped. */
  placement: QuickButtonPlacement;
}

/** Renders every active extension's quick button placed on this surface. */
export function ExtensionQuickButtons({ context, className = "", variant, placement }: QuickButtonProps) {
  const { quick_buttons } = useExtensionUiHooks();
  const placed = quick_buttons.filter((qb) => (qb.placements ?? []).includes(placement));
  const cls = ["extension-quick-buttons", `extension-quick-buttons--${variant}`, className]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={cls}>
      {placed.map((qb) => {
        const moduleUrl = qb.action.type === "module" ? qb.action.module_url : "";
        if (moduleUrl) {
          return (
            <ExtensionModuleSlot
              key={`${qb.extension_id}:quick-button`}
              module={{
                extension_id: qb.extension_id,
                extension_name: qb.extension_name,
                slot: "quick-button",
                id: "quick-button",
                label: qb.label,
                kind: "module",
                module_url: moduleUrl,
                payments: false,
              }}
              className={`extension-module-slot--${variant} extension-quick-button--module`}
              context={{ ...context, variant }}
            />
          );
        }
        return (
          <button
            key={`${qb.extension_id}:quick-button`}
            type="button"
            className={`extension-quick-button extension-quick-button--${variant} ${quickButtonIconClass(qb.icon)}`}
            title={qb.label}
            aria-label={qb.label}
            onClick={() => void runHookAction(qb.action, context)}
          >
            <HookGlyph icon={qb.icon} label={qb.label} size={15} />
            {variant === "toolbar" && <span className="extension-quick-button-label">{qb.label}</span>}
          </button>
        );
      })}
    </span>
  );
}

interface PageIconsProps {
  context: HookActionContext;
}

/** Renders every active extension's page icon (with optional number badge) in
 *  the sidebar header. */
export function ExtensionPageIcons({ context }: PageIconsProps) {
  const { pages } = useExtensionUiHooks();
  const badges = useExtensionPageBadges(pages);
  return (
    <>
      {pages.map((page) => {
        const count = badges[`${page.extension_id}:${page.id}`] ?? 0;
        const title = count > 0 ? `${page.label} (${count})` : page.label;
        return (
          <button
            key={`${page.extension_id}:${page.id}`}
            type="button"
            className="setup-btn extension-page-icon"
            title={title}
            aria-label={title}
            style={count > 0 ? { position: "relative" } : undefined}
            onClick={() => void runHookAction(page.open, context)}
          >
            <HookGlyph icon={page.icon} label={page.label} size={15} />
            {count > 0 && <span className="extension-page-icon-badge">{count}</span>}
          </button>
        );
      })}
    </>
  );
}
