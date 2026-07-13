import * as ReactRuntime from "react";
import { createContext, createElement, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import { useTranslation } from "react-i18next";
import { API } from "src/api";
import { eventBus } from "src/lib/eventBus";
import { logDurable } from "src/lib/frontendLogger";
import { disposeSharedSnapshotScope } from "src/lib/sharedSnapshotPoller";
import { uuidv4 } from "src/lib/uuid";
import { disposeExtensionModules, loadExtensionModule } from "./extensionModuleLoader";
import {
  beginExtensionMountWindow,
  disposeExtensionRuntime,
  scheduleExtensionMount,
} from "./extensionRuntimePerformance";
import { ExtensionPaymentModal, type ExtensionPaymentResult } from "./ExtensionPaymentModal";

export interface ExtensionFrontendModule {
  extension_id: string;
  extension_name: string;
  slot: string;
  id: string;
  label: string;
  kind: string;
  module_url: string;
  payments: boolean;
  marketplace_auth?: boolean;
}

interface FrontendEntrypointPayload {
  entrypoints?: Array<{
    extension_id?: unknown;
    name?: unknown;
    payments?: unknown;
    marketplace_auth?: unknown;
    frontend_modules?: Array<{
      slot?: unknown;
      id?: unknown;
      label?: unknown;
      kind?: unknown;
      module_url?: unknown;
    }>;
  }>;
}

export interface ExtensionCatalogError {
  code: string;
  resetAvailable: boolean;
  foundSchema: number | null;
  revision: string;
}

export interface ExtensionFrontendCatalog {
  modules: ExtensionFrontendModule[];
  error: ExtensionCatalogError | null;
  resetting: boolean;
  resetError: boolean;
  reset: () => Promise<void>;
}

interface ExtensionMountContext {
  apiBaseUrl: string;
  authScopeKey: string;
  extensionId: string;
  extensionName: string;
  slot: string;
  moduleId: string;
  [key: string]: unknown;
}

const ExtensionAuthScopeContext = createContext("");
let authScopeGeneration = 0;
let activeAuthScopeKey = "";
const authScopeDisposalTimers = new Map<string, number>();

export function ExtensionAuthScopeProvider({ authStatus, username, children }: {
  authStatus: string;
  username: string | null;
  children: ReactRuntime.ReactNode;
}) {
  const authScopeKey = useMemo(() => `scope-${++authScopeGeneration}`, [authStatus, username]);
  activeAuthScopeKey = authScopeKey;
  useEffect(() => {
    const pendingDisposal = authScopeDisposalTimers.get(authScopeKey);
    if (pendingDisposal !== undefined) {
      window.clearTimeout(pendingDisposal);
      authScopeDisposalTimers.delete(authScopeKey);
    }
    const store = catalogStoreFor(authScopeKey);
    void store.refresh();
    const off = eventBus.subscribe("extensions_changed", () => void store.refresh(true));
    return () => {
      off();
      const timer = window.setTimeout(() => {
        authScopeDisposalTimers.delete(authScopeKey);
        store.dispose();
        catalogStores.delete(authScopeKey);
        disposeExtensionModules(authScopeKey);
        disposeExtensionRuntime(authScopeKey);
        disposeSharedSnapshotScope(authScopeKey);
        window.dispatchEvent(new CustomEvent("extension_auth_scope_disposed", {
          detail: { authScopeKey },
        }));
      }, 0);
      authScopeDisposalTimers.set(authScopeKey, timer);
    };
  }, [authScopeKey]);
  return createElement(ExtensionAuthScopeContext.Provider, { value: authScopeKey }, children);
}

export function useExtensionAuthScope(): string { return useContext(ExtensionAuthScopeContext); }
export function getActiveExtensionAuthScope(): string { return activeAuthScopeKey; }

type ExtensionCleanup = void | (() => void) | { unmount: () => void };
type ExtensionMount = (args: {
  container: HTMLElement;
  context: ExtensionMountContext;
}) => ExtensionCleanup | Promise<ExtensionCleanup>;
type ExtensionComponent = (props: {
  context: ExtensionMountContext;
  React: typeof ReactRuntime;
}) => ReactRuntime.ReactNode;
type ExtensionModule = {
  mount?: ExtensionMount;
  default?: ExtensionMount;
  Component?: ExtensionComponent;
};
type MountedKind = "component" | "mount";

const EMPTY_EXTENSION_CONTEXT: Record<string, unknown> = Object.freeze({});
const EAGER_EXTENSION_SLOTS = new Set([
  "global-approval-overlay", "session-drag-overlay", "session-action-modal",
  "session-workspace-overlay", "input-overflow-menu", "composer-actions", "chat-inline-actions",
]);
const MOUNT_PRIORITY: Record<string, number> = {
  "session-toolbar": 0,
  "mobile-session-topbar": 0,
  "team-sidebar": 10,
  "routines-sidebar": 10,
  "sidebar-scope-tabs": 10,
  "right-panel-canvas": 20,
  "right-panel-screen": 20,
  "extension-panel": 30,
  "route-page": 30,
};
const EXTENSION_ID_SEGMENT = "[A-Za-z0-9][A-Za-z0-9._-]{0,127}";

function sameExtensionContext(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  if (left === right) return true;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => Object.hasOwn(right, key) && Object.is(left[key], right[key]));
}

function useStableExtensionContext(context: Record<string, unknown>): Record<string, unknown> {
  const stableRef = useRef(context);
  if (!sameExtensionContext(stableRef.current, context)) stableRef.current = context;
  return stableRef.current;
}
const MARKETPLACE_REQUEST_RULES = [
  { method: "GET", path: /^\/api\/extensions$/ },
  { method: "POST", path: /^\/api\/extensions\/install$/ },
  { method: "PATCH", path: new RegExp(`^/api/extensions/${EXTENSION_ID_SEGMENT}/enabled$`) },
  { method: "DELETE", path: new RegExp(`^/api/extensions/${EXTENSION_ID_SEGMENT}$`) },
  { method: "GET", path: /^\/api\/extensions\/ofek-dev\.marketplace\/backend\/auth\/(providers|status)$/ },
  { method: "POST", path: /^\/api\/extensions\/ofek-dev\.marketplace\/backend\/auth\/logout$/ },
  { method: "GET", path: /^\/api\/extensions\/ofek-dev\.marketplace\/backend\/catalog$/ },
  { method: "GET", path: new RegExp(`^/api/extensions/ofek-dev\\.marketplace/backend/metadata/${EXTENSION_ID_SEGMENT}$`) },
  { method: "POST", path: new RegExp(`^/api/extensions/ofek-dev\\.marketplace/backend/extensions/${EXTENSION_ID_SEGMENT}/uninstall$`) },
] as const;

function isAllowedMarketplaceRequest(path: string, method: string): boolean {
  return MARKETPLACE_REQUEST_RULES.some((rule) => rule.method === method && rule.path.test(path));
}

function normalizeModuleUrl(moduleUrl: string): string {
  if (/^https?:\/\//.test(moduleUrl) || moduleUrl.startsWith("//")) {
    throw new Error("Extension module URL must be a backend-served package asset");
  }
  if (!moduleUrl.startsWith("/api/extensions/") || !moduleUrl.includes("/frontend/")) {
    throw new Error("Extension module URL must be an extension frontend asset");
  }
  return `${API}${moduleUrl.startsWith("/") ? "" : "/"}${moduleUrl}`;
}

export function iframeModuleUrl(moduleUrl: string): string {
  return normalizeModuleUrl(moduleUrl);
}

function cleanupMounted(result: ExtensionCleanup): void {
  if (typeof result === "function") {
    result();
    return;
  }
  if (result && typeof result.unmount === "function") {
    result.unmount();
  }
}

function flattenModules(payload: FrontendEntrypointPayload, slot?: string): ExtensionFrontendModule[] {
  const entrypoints = Array.isArray(payload.entrypoints) ? payload.entrypoints : [];
  const modules: ExtensionFrontendModule[] = [];
  for (const entrypoint of entrypoints) {
    const extensionId = typeof entrypoint.extension_id === "string" ? entrypoint.extension_id : "";
    const extensionName = typeof entrypoint.name === "string" ? entrypoint.name : extensionId;
    if (!extensionId) continue;
    const frontendModules = Array.isArray(entrypoint.frontend_modules) ? entrypoint.frontend_modules : [];
    for (const item of frontendModules) {
      if (slot && item.slot !== slot) continue;
      if (
        typeof item.id !== "string" ||
        typeof item.label !== "string" ||
        typeof item.module_url !== "string"
      ) {
        continue;
      }
      modules.push({
        extension_id: extensionId,
        extension_name: extensionName,
        slot: String(item.slot),
        id: item.id,
        label: item.label,
        kind: typeof item.kind === "string" && item.kind ? item.kind : "module",
        module_url: item.module_url,
        payments: entrypoint.payments === true,
        marketplace_auth: entrypoint.marketplace_auth === true,
      });
    }
  }
  return modules;
}

interface CatalogSnapshot {
  modules: ExtensionFrontendModule[];
  error: ExtensionCatalogError | null;
  resetting: boolean;
  resetError: boolean;
}

const EMPTY_CATALOG_SNAPSHOT: CatalogSnapshot = Object.freeze({
  modules: [], error: null, resetting: false, resetError: false,
});

class ExtensionCatalogStore {
  readonly scopeKey: string;
  private snapshot: CatalogSnapshot = EMPTY_CATALOG_SNAPSHOT;
  private listeners = new Set<() => void>();
  private generation = 0;
  private inflight: Promise<void> | null = null;
  private controller: AbortController | null = null;

  constructor(scopeKey: string) {
    this.scopeKey = scopeKey;
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = () => this.snapshot;

  private commit(next: CatalogSnapshot) {
    this.snapshot = next;
    for (const listener of this.listeners) listener();
  }

  refresh(force = false): Promise<void> {
    if (this.inflight && !force) return this.inflight;
    if (force) {
      this.controller?.abort();
      this.inflight = null;
    }
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller?.abort();
    this.controller = controller;
    const queuedAt = performance.now();
    this.inflight = (async () => {
      const requestAt = performance.now();
      const queueMs = requestAt - queuedAt;
      if (queueMs >= 50) logDurable("extensions.catalog", "queue", { duration_ms: Math.round(queueMs), generation, scope: this.scopeKey });
      try {
        const response = await fetch(`${API}/api/extensions/frontend-entrypoints`, {
          credentials: "include", signal: controller.signal,
        });
        const headersAt = performance.now();
        const ttfbMs = headersAt - requestAt;
        if (ttfbMs >= 200) logDurable("extensions.catalog", "ttfb", { duration_ms: Math.round(ttfbMs), generation, status: response.status });
        const text = await response.text();
        const downloadedAt = performance.now();
        const downloadMs = downloadedAt - headersAt;
        if (downloadMs >= 50) logDurable("extensions.catalog", "download", { duration_ms: Math.round(downloadMs), generation, bytes: new Blob([text]).size });
        const parseAt = performance.now();
        const payload = JSON.parse(text || "{}") as FrontendEntrypointPayload & { detail?: Record<string, unknown> };
        const jsonMs = performance.now() - parseAt;
        if (jsonMs >= 50) logDurable("extensions.catalog", "json", { duration_ms: Math.round(jsonMs), generation });
        if (!response.ok) {
          const detail = payload.detail;
          const requestError = new Error(`HTTP ${response.status}`) as Error & { catalogError?: ExtensionCatalogError };
          requestError.catalogError = {
            code: typeof detail?.error === "string" ? detail.error : "extension_catalog_unavailable",
            resetAvailable: detail?.reset_available === true,
            foundSchema: typeof detail?.found_schema === "number" ? detail.found_schema : null,
            revision: typeof detail?.revision === "string" ? detail.revision : "",
          };
          throw requestError;
        }
        if (generation !== this.generation || controller.signal.aborted) return;
        const modules = flattenModules(payload);
        const commitAt = performance.now();
        this.commit({ modules, error: null, resetting: false, resetError: false });
        const commitMs = performance.now() - commitAt;
        if (commitMs >= 50) logDurable("extensions.catalog", "commit", {
          duration_ms: Math.round(commitMs), generation,
          modules: modules.length, unique_urls: new Set(modules.map((item) => item.module_url)).size,
          subscribers: this.listeners.size,
        });
      } catch (requestError) {
        if (controller.signal.aborted || generation !== this.generation) return;
        const catalogError = (requestError as Error & { catalogError?: ExtensionCatalogError }).catalogError;
        this.commit({
          modules: [], resetting: false, resetError: false,
          error: catalogError ?? { code: "extension_catalog_unavailable", resetAvailable: false, foundSchema: null, revision: "" },
        });
      }
    })().finally(() => {
      if (generation === this.generation) this.inflight = null;
    });
    return this.inflight;
  }

  async reset(): Promise<void> {
    const { error } = this.snapshot;
    if (this.snapshot.resetting || !error?.resetAvailable || !error.revision) return;
    this.commit({ ...this.snapshot, resetting: true, resetError: false });
    try {
      const response = await fetch(`${API}/api/extensions/settings/reset`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_found_schema: error.foundSchema, expected_revision: error.revision }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await this.refresh();
    } catch {
      this.commit({ ...this.snapshot, resetting: false, resetError: true });
    }
  }

  dispose() {
    this.generation += 1;
    this.controller?.abort();
    this.inflight = null;
  }
}

const catalogStores = new Map<string, ExtensionCatalogStore>();
function catalogStoreFor(scopeKey: string): ExtensionCatalogStore {
  let store = catalogStores.get(scopeKey);
  if (!store) {
    store = new ExtensionCatalogStore(scopeKey);
    catalogStores.set(scopeKey, store);
  }
  return store;
}

export function useExtensionFrontendCatalog(slot: string): ExtensionFrontendCatalog {
  const scopeKey = useContext(ExtensionAuthScopeContext) || "anonymous";
  const store = useMemo(() => catalogStoreFor(scopeKey), [scopeKey]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  useEffect(() => {
    void store.refresh();
    if (scopeKey !== "anonymous") return undefined;
    return eventBus.subscribe("extensions_changed", () => void store.refresh(true));
  }, [scopeKey, store]);
  return {
    ...snapshot,
    modules: useMemo(() => snapshot.modules.filter((module) => module.slot === slot), [snapshot.modules, slot]),
    reset: useCallback(() => store.reset(), [store]),
  };
}

export function useExtensionFrontendModules(slot: string): ExtensionFrontendModule[] {
  return useExtensionFrontendCatalog(slot).modules;
}

export function ExtensionCatalogRecovery({
  catalog,
}: {
  catalog: Pick<ExtensionFrontendCatalog, "error" | "resetting" | "resetError" | "reset">;
}) {
  const { t } = useTranslation();
  if (!catalog.error) return null;
  return (
    <div className="extension-catalog-error" role="alert">
      <span>{t("extensions.catalogUnavailable")}</span>
      {catalog.error.resetAvailable && (
        <button
          type="button"
          className="extension-catalog-reset"
          disabled={catalog.resetting}
          onClick={() => void catalog.reset()}
        >
          {catalog.resetting
            ? t("extensions.resettingSettings")
            : t("extensions.resetSettings")}
        </button>
      )}
      {catalog.error.resetAvailable && (
        <small>{t("extensions.resetSettingsWarning")}</small>
      )}
      {catalog.resetError && (
        <small className="extension-catalog-reset-error">
          {t("extensions.resetSettingsFailed")}
        </small>
      )}
    </div>
  );
}

export function ExtensionModuleSlot({
  module,
  className = "",
  context = EMPTY_EXTENSION_CONTEXT,
}: {
  module: ExtensionFrontendModule;
  className?: string;
  context?: Record<string, unknown>;
}) {
  const authScopeKey = useContext(ExtensionAuthScopeContext);
  const stableContext = useStableExtensionContext(context);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cleanupRef = useRef<ExtensionCleanup>(undefined);
  const mountWindowReleaseRef = useRef<(() => void) | null>(null);
  const mountedKindRef = useRef<MountedKind | null>(null);
  const rootRef = useRef<ReturnType<typeof createRoot> | null>(null);
  const componentRef = useRef<ExtensionComponent | null>(null);
  const contextRef = useRef<Record<string, unknown>>(stableContext);
  contextRef.current = stableContext;
  const [error, setError] = useState("");
  const [mountReady, setMountReady] = useState(() => EAGER_EXTENSION_SLOTS.has(module.slot));
  const moduleUrlResult = useMemo(() => {
    try {
      return { url: normalizeModuleUrl(module.module_url), error: "" };
    } catch (e) {
      return {
        url: "",
        error: e instanceof Error ? e.message : "Extension module URL is invalid",
      };
    }
  }, [module.module_url]);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const authPopupRef = useRef<Window | null>(null);
  const authStateRef = useRef("");
  const bridgeNonceRef = useRef(uuidv4());
  const [paymentRequest, setPaymentRequest] = useState<{ requestId: string; productId: string } | null>(null);

  useEffect(() => {
    if (EAGER_EXTENSION_SLOTS.has(module.slot)) return undefined;
    return scheduleExtensionMount(
      authScopeKey,
      `${module.extension_id}/${module.id}`,
      MOUNT_PRIORITY[module.slot] ?? 40,
      () => setMountReady(true),
    );
  }, [authScopeKey, module.extension_id, module.id, module.slot, module.module_url]);

  const postToIframe = useCallback((payload: Record<string, unknown>) => {
    iframeRef.current?.contentWindow?.postMessage({ source: "ba-core", nonce: bridgeNonceRef.current, ...payload }, "*");
  }, []);

  useEffect(() => {
    if (module.kind !== "iframe" || (!module.payments && !module.marketplace_auth)) return undefined;

    async function handleAuthStart(requestId: string, provider: unknown) {
      try {
        const response = await fetch(
          `${API}/api/extensions/${encodeURIComponent(module.extension_id)}/backend/auth/start`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: String(provider ?? "") }),
          },
        );
        if (!response.ok) throw new Error(await response.text());
        const payload = (await response.json()) as { login_url?: string; state?: string };
        if (!payload.login_url || !payload.state) throw new Error("missing login state");
        const popup = window.open(payload.login_url, "_blank", "popup");
        if (!popup) throw new Error("sign-in popup was blocked");
        authPopupRef.current = popup;
        authStateRef.current = payload.state;
        postToIframe({ requestId, status: "pending" });
      } catch (e) {
        postToIframe({ requestId, ok: false, error: e instanceof Error ? e.message : String(e) });
      }
    }

    async function handleMarketplaceRequest(requestId: string, path: unknown, requestedMethod: unknown, body: unknown) {
      const method = String(requestedMethod || "GET").toUpperCase();
      if (typeof path !== "string" || !isAllowedMarketplaceRequest(path, method)) {
        postToIframe({ action: "marketplace-response", requestId, ok: false, error: "marketplace request denied" });
        return;
      }
      try {
        const response = await fetch(`${API}${path}`, {
          method,
          credentials: "include",
          headers: body === undefined ? undefined : { "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        const text = await response.text();
        let payload: unknown = null;
        if (text) {
          try {
            payload = JSON.parse(text);
          } catch {
            payload = text;
          }
        }
        postToIframe({
          action: "marketplace-response",
          requestId,
          ok: response.ok,
          payload,
          error: response.ok ? "" : (typeof payload === "string" ? payload : `request failed (${response.status})`),
        });
      } catch (e) {
        postToIframe({ action: "marketplace-response", requestId, ok: false, error: e instanceof Error ? e.message : String(e) });
      }
    }

    function onMessage(event: MessageEvent) {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data as { source?: unknown; nonce?: unknown; action?: unknown; requestId?: unknown; provider?: unknown; productId?: unknown; state?: unknown; path?: unknown; method?: unknown; body?: unknown };
      if (!data || data.source !== "ba-extension" || data.nonce !== bridgeNonceRef.current || typeof data.requestId !== "string") return;
      if (data.action === "marketplace-auth-start" && module.marketplace_auth) {
        void handleAuthStart(data.requestId, data.provider);
        return;
      }
      if (data.action === "marketplace-request" && module.marketplace_auth) {
        void handleMarketplaceRequest(data.requestId, data.path, data.method, data.body);
        return;
      }
      if (data.action === "marketplace-purchase" && module.payments) {
        const productId = String(data.productId ?? "");
        if (!productId) {
          postToIframe({ requestId: data.requestId, status: "failed", error: "missing product id" });
          return;
        }
        setPaymentRequest({ requestId: data.requestId, productId });
      }
    }

    function onAuthComplete(event: MessageEvent) {
      if (event.source !== authPopupRef.current) return;
      const data = event.data as { source?: unknown; state?: unknown };
      if (data?.source !== "better-agent-marketplace-auth" || data.state !== authStateRef.current) return;
      authPopupRef.current = null;
      authStateRef.current = "";
      postToIframe({ action: "marketplace-auth-result", status: "authenticated" });
    }

    function onFocus() {
      if (!authPopupRef.current?.closed) return;
      authPopupRef.current = null;
      authStateRef.current = "";
      postToIframe({ action: "marketplace-auth-result", status: "cancelled" });
    }

    window.addEventListener("message", onMessage);
    window.addEventListener("message", onAuthComplete);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("message", onAuthComplete);
      window.removeEventListener("focus", onFocus);
      authPopupRef.current?.close();
      authPopupRef.current = null;
    };
  }, [module.kind, module.payments, module.marketplace_auth, module.extension_id, postToIframe]);

  const onPaymentDone = useCallback(
    (result: ExtensionPaymentResult) => {
      setPaymentRequest((current) => {
        if (current) {
          postToIframe({
            requestId: current.requestId,
            status: result.status,
            entitlementToken: result.entitlementToken ?? "",
            error: result.error ?? "",
          });
        }
        return null;
      });
    },
    [postToIframe],
  );

  const buildMountContext = useCallback(
    (): ExtensionMountContext => ({
      apiBaseUrl: API,
      authScopeKey,
      extensionId: module.extension_id,
      extensionName: module.extension_name,
      slot: module.slot,
      moduleId: module.id,
      ...contextRef.current,
    }),
    [authScopeKey, module.extension_id, module.extension_name, module.slot, module.id],
  );

  useLayoutEffect(() => {
    if (module.kind === "iframe") return undefined;
    if (!mountReady) return undefined;
    if (moduleUrlResult.error) {
      setError(moduleUrlResult.error);
      return undefined;
    }
    let cancelled = false;
    const container = containerRef.current;
    if (!container) return undefined;
    const targetContainer: HTMLElement = container;
    setError("");

    async function mountModule() {
      const startedAt = performance.now();
      const finishWindow = beginExtensionMountWindow(
        authScopeKey,
        `${module.extension_id}/${module.id}`,
      );
      mountWindowReleaseRef.current = finishWindow;
      try {
        const imported = (await loadExtensionModule(moduleUrlResult.url, authScopeKey)) as ExtensionModule;
        if (cancelled) return;
        const mountAt = performance.now();
        if (typeof imported.Component === "function") {
          const root = createRoot(targetContainer);
          const component = imported.Component;
          root.render(createElement(component, { context: buildMountContext(), React: ReactRuntime }));
          rootRef.current = root;
          componentRef.current = component;
          mountedKindRef.current = "component";
          cleanupRef.current = () => root.unmount();
        } else {
          const mount = imported.mount ?? imported.default;
          if (typeof mount !== "function") {
            throw new Error(`Extension module ${module.extension_id}/${module.id} has no mount export`);
          }
          const cleanup = await mount({
            container: targetContainer,
            context: buildMountContext(),
          });
          if (cancelled) {
            cleanupMounted(cleanup);
            return;
          }
          mountedKindRef.current = "mount";
          cleanupRef.current = cleanup;
        }
        const mountMs = performance.now() - mountAt;
        if (mountMs >= 50) logDurable("extensions.module", "mount", {
          extension_id: module.extension_id,
          module_id: module.id,
          slot: module.slot,
          duration_ms: Math.round(mountMs),
        });
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (cancelled) return;
          const paintMs = performance.now() - startedAt;
          if (paintMs >= 100) logDurable("extensions.module", "paint", {
            extension_id: module.extension_id,
            module_id: module.id,
            slot: module.slot,
            duration_ms: Math.round(paintMs),
          });
        }));
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Extension module failed to load");
        }
      } finally {
        finishWindow();
        if (mountWindowReleaseRef.current === finishWindow) mountWindowReleaseRef.current = null;
      }
    }

    void mountModule();
    return () => {
      cancelled = true;
      mountWindowReleaseRef.current?.();
      mountWindowReleaseRef.current = null;
      cleanupMounted(cleanupRef.current);
      cleanupRef.current = undefined;
      if (mountedKindRef.current === "mount") {
        targetContainer.replaceChildren();
      }
      mountedKindRef.current = null;
      rootRef.current = null;
      componentRef.current = null;
    };
  }, [module.extension_id, module.extension_name, module.id, module.kind, module.slot, moduleUrlResult, buildMountContext, authScopeKey, mountReady]);

  useEffect(() => {
    const root = rootRef.current;
    const component = componentRef.current;
    if (!root || !component) return;
    root.render(createElement(component, { context: buildMountContext(), React: ReactRuntime }));
  }, [stableContext, buildMountContext]);

  const classes = ["extension-module-slot", className].filter(Boolean).join(" ");

  if (moduleUrlResult.error) {
    return <div className="setup-error">{moduleUrlResult.error}</div>;
  }

  if (module.kind === "iframe") {
    return (
      <>
        <iframe
          ref={iframeRef}
          className={`${classes} extension-module-slot--iframe`}
          src={moduleUrlResult.url}
          title={module.label || module.id}
          // No allow-same-origin: the bundle is served same-origin, so granting it
          // would let extension script reach the app's cookies/storage/parent DOM.
          // The iframe runs in an opaque origin — fully isolated from the host app.
          sandbox="allow-scripts allow-forms"
          onLoad={() => postToIframe({ action: "marketplace-auth-init" })}
        />
        {module.payments && paymentRequest && (
          <ExtensionPaymentModal
            open
            extensionId={module.extension_id}
            productId={paymentRequest.productId}
            onDone={onPaymentDone}
          />
        )}
      </>
    );
  }

  return (
    <>
      <div className={classes} ref={containerRef} aria-busy={!mountReady || undefined} />
      {error && <div className="setup-error">{error}</div>}
    </>
  );
}
