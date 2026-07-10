import * as ReactRuntime from "react";
import { createElement, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { useTranslation } from "react-i18next";
import { API } from "src/api";
import { eventBus } from "src/lib/eventBus";
import { trackPromise } from "src/progress/store";
import { loadExtensionModule } from "./extensionModuleLoader";
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
  extensionId: string;
  extensionName: string;
  slot: string;
  moduleId: string;
  [key: string]: unknown;
}

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

function flattenModules(payload: FrontendEntrypointPayload, slot: string): ExtensionFrontendModule[] {
  const entrypoints = Array.isArray(payload.entrypoints) ? payload.entrypoints : [];
  const modules: ExtensionFrontendModule[] = [];
  for (const entrypoint of entrypoints) {
    const extensionId = typeof entrypoint.extension_id === "string" ? entrypoint.extension_id : "";
    const extensionName = typeof entrypoint.name === "string" ? entrypoint.name : extensionId;
    if (!extensionId) continue;
    const frontendModules = Array.isArray(entrypoint.frontend_modules) ? entrypoint.frontend_modules : [];
    for (const item of frontendModules) {
      if (item.slot !== slot) continue;
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
        slot,
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

export function useExtensionFrontendCatalog(slot: string): ExtensionFrontendCatalog {
  const [modules, setModules] = useState<ExtensionFrontendModule[]>([]);
  const [error, setError] = useState<ExtensionCatalogError | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState(false);

  const refresh = useCallback(async () => {
    const { promise } = trackPromise(`extensions:frontend-modules:${slot}`, async () => {
      const response = await fetch(`${API}/api/extensions/frontend-entrypoints`, {
        credentials: "include",
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as {
          detail?: {
            error?: unknown;
            reset_available?: unknown;
            found_schema?: unknown;
            revision?: unknown;
          };
        };
        const detail = payload.detail;
        const requestError = new Error(`HTTP ${response.status}`) as Error & {
          catalogError?: ExtensionCatalogError;
        };
        requestError.catalogError = {
          code: typeof detail?.error === "string" ? detail.error : "extension_catalog_unavailable",
          resetAvailable: detail?.reset_available === true,
          foundSchema: typeof detail?.found_schema === "number" ? detail.found_schema : null,
          revision: typeof detail?.revision === "string" ? detail.revision : "",
        };
        throw requestError;
      }
      return (await response.json()) as FrontendEntrypointPayload;
    });
    try {
      setModules(flattenModules(await promise, slot));
      setError(null);
      setResetError(false);
    } catch (requestError) {
      const catalogError = (requestError as Error & { catalogError?: ExtensionCatalogError }).catalogError;
      setError(catalogError ?? {
        code: "extension_catalog_unavailable",
        resetAvailable: false,
        foundSchema: null,
        revision: "",
      });
    }
  }, [slot]);

  const reset = useCallback(async () => {
    if (resetting || !error?.resetAvailable || !error.revision) return;
    setResetting(true);
    setResetError(false);
    try {
      const response = await fetch(`${API}/api/extensions/settings/reset`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_found_schema: error.foundSchema,
          expected_revision: error.revision,
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await refresh();
    } catch {
      setResetError(true);
    } finally {
      setResetting(false);
    }
  }, [error, refresh, resetting]);

  useEffect(() => {
    void refresh();
    const off = eventBus.subscribe("extensions_changed", () => {
      void refresh();
    });
    return off;
  }, [refresh]);

  return { modules, error, resetting, resetError, reset };
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
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cleanupRef = useRef<ExtensionCleanup>(undefined);
  const mountedKindRef = useRef<MountedKind | null>(null);
  const rootRef = useRef<ReturnType<typeof createRoot> | null>(null);
  const componentRef = useRef<ExtensionComponent | null>(null);
  const contextRef = useRef<Record<string, unknown>>(context);
  contextRef.current = context;
  const [error, setError] = useState("");
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
  const bridgeNonceRef = useRef(crypto.randomUUID());
  const [paymentRequest, setPaymentRequest] = useState<{ requestId: string; productId: string } | null>(null);

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

    function onMessage(event: MessageEvent) {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data as { source?: unknown; nonce?: unknown; action?: unknown; requestId?: unknown; provider?: unknown; productId?: unknown; state?: unknown };
      if (!data || data.source !== "ba-extension" || data.nonce !== bridgeNonceRef.current || typeof data.requestId !== "string") return;
      if (data.action === "marketplace-auth-start" && module.marketplace_auth) {
        void handleAuthStart(data.requestId, data.provider);
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
      extensionId: module.extension_id,
      extensionName: module.extension_name,
      slot: module.slot,
      moduleId: module.id,
      ...contextRef.current,
    }),
    [module.extension_id, module.extension_name, module.slot, module.id],
  );

  useLayoutEffect(() => {
    if (module.kind === "iframe") return undefined;
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
      try {
        const imported = (await loadExtensionModule(moduleUrlResult.url)) as ExtensionModule;
        if (cancelled) return;
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
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Extension module failed to load");
        }
      }
    }

    void mountModule();
    return () => {
      cancelled = true;
      cleanupMounted(cleanupRef.current);
      cleanupRef.current = undefined;
      if (mountedKindRef.current === "mount") {
        targetContainer.replaceChildren();
      }
      mountedKindRef.current = null;
      rootRef.current = null;
      componentRef.current = null;
    };
  }, [module.extension_id, module.extension_name, module.id, module.kind, module.slot, moduleUrlResult, buildMountContext]);

  useEffect(() => {
    const root = rootRef.current;
    const component = componentRef.current;
    if (!root || !component) return;
    root.render(createElement(component, { context: buildMountContext(), React: ReactRuntime }));
  }, [context, buildMountContext]);

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
      <div className={classes} ref={containerRef} />
      {error && <div className="setup-error">{error}</div>}
    </>
  );
}
