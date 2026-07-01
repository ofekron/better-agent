import * as ReactRuntime from "react";
import { createElement, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { API } from "src/api";
import { eventBus } from "src/lib/eventBus";
import { trackPromise } from "src/progress/store";
import { loadExtensionModule } from "./extensionModuleLoader";

export interface ExtensionFrontendModule {
  extension_id: string;
  extension_name: string;
  slot: string;
  id: string;
  label: string;
  kind: string;
  module_url: string;
}

interface FrontendEntrypointPayload {
  entrypoints?: Array<{
    extension_id?: unknown;
    name?: unknown;
    frontend_modules?: Array<{
      slot?: unknown;
      id?: unknown;
      label?: unknown;
      kind?: unknown;
      module_url?: unknown;
    }>;
  }>;
}

interface ExtensionMountContext {
  apiBaseUrl: string;
  extensionId: string;
  extensionName: string;
  slot: string;
  moduleId: string;
  subscribeToEvent: (type: string, handler: (payload: unknown) => void) => () => void;
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
const HOST_EXTENSION_EVENTS = new Set(["extensions_changed", "websocket.connected"]);

function extensionEventPrefix(extensionId: string): string {
  const parts = extensionId.split(".").filter(Boolean);
  const localName = parts[parts.length - 1] || extensionId;
  return `${localName}.`;
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
      });
    }
  }
  return modules;
}

export function useExtensionFrontendModules(slot: string): ExtensionFrontendModule[] {
  const [modules, setModules] = useState<ExtensionFrontendModule[]>([]);

  const refresh = useCallback(async () => {
    const { promise } = trackPromise(`extensions:frontend-modules:${slot}`, async () => {
      const response = await fetch(`${API}/api/extensions/frontend-entrypoints`, {
        credentials: "include",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return (await response.json()) as FrontendEntrypointPayload;
    });
    try {
      setModules(flattenModules(await promise, slot));
    } catch {
      setModules([]);
    }
  }, [slot]);

  useEffect(() => {
    void refresh();
    const off = eventBus.subscribe("extensions_changed", () => {
      void refresh();
    });
    return off;
  }, [refresh]);

  return modules;
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
  const moduleUrl = useMemo(() => normalizeModuleUrl(module.module_url), [module.module_url]);
  const eventPrefix = useMemo(() => extensionEventPrefix(module.extension_id), [module.extension_id]);
  const subscribeToEvent = useCallback(
    (type: string, handler: (payload: unknown) => void) => {
      if (!HOST_EXTENSION_EVENTS.has(type) && !type.startsWith(eventPrefix)) {
        return () => {};
      }
      return eventBus.subscribe(type, handler);
    },
    [eventPrefix],
  );

  const buildMountContext = useCallback(
    (): ExtensionMountContext => ({
      apiBaseUrl: API,
      extensionId: module.extension_id,
      extensionName: module.extension_name,
      slot: module.slot,
      moduleId: module.id,
      ...contextRef.current,
      subscribeToEvent,
    }),
    [module.extension_id, module.extension_name, module.slot, module.id, subscribeToEvent],
  );

  useLayoutEffect(() => {
    if (module.kind === "iframe") return undefined;
    let cancelled = false;
    const container = containerRef.current;
    if (!container) return undefined;
    const targetContainer: HTMLElement = container;
    setError("");

    async function mountModule() {
      try {
        const imported = (await loadExtensionModule(moduleUrl)) as ExtensionModule;
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
  }, [module.extension_id, module.extension_name, module.id, module.kind, module.slot, moduleUrl, buildMountContext]);

  useEffect(() => {
    const root = rootRef.current;
    const component = componentRef.current;
    if (!root || !component) return;
    root.render(createElement(component, { context: buildMountContext(), React: ReactRuntime }));
  }, [context, buildMountContext]);

  const classes = ["extension-module-slot", className].filter(Boolean).join(" ");

  if (module.kind === "iframe") {
    const iframeUrl = iframeModuleUrl(module.module_url);
    return (
      <iframe
        className={`${classes} extension-module-slot--iframe`}
        src={iframeUrl}
        title={module.label || module.id}
        // No allow-same-origin: the bundle is served same-origin, so granting it
        // would let extension script reach the app's cookies/storage/parent DOM.
        // The iframe runs in an opaque origin — fully isolated from the host app.
        sandbox="allow-scripts allow-forms"
      />
    );
  }

  return (
    <>
      <div className={classes} ref={containerRef} />
      {error && <div className="setup-error">{error}</div>}
    </>
  );
}
