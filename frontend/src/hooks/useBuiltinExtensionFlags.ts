import { useCallback, useEffect, useState } from "react";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { builtinIdsLoaded, extId, loadBuiltinExtensionIds } from "../extensionIds";

const BUILTIN_FLAG_KEYS = [
  "ask",
  "team",
  "supervisor",
  "projectStructure",
  "machineNodes",
  "credentialBroker",
  "providerConfigSync",
  "canvas",
  "promptEngineer",
  "browserHarness",
  "testape",
  "routines",
] as const;

export type BuiltinExtensionFlags = Record<(typeof BUILTIN_FLAG_KEYS)[number], boolean>;

const DEFAULT_BUILTIN_EXTENSION_FLAGS: BuiltinExtensionFlags = {
  ask: true,
  team: true,
  supervisor: true,
  projectStructure: true,
  machineNodes: true,
  credentialBroker: true,
  providerConfigSync: true,
  canvas: true,
  promptEngineer: true,
  browserHarness: true,
  testape: true,
  routines: true,
};

export function useBuiltinExtensionFlags(
  authStatus: "loading" | "authed",
): BuiltinExtensionFlags {
  const [flags, setFlags] = useState<BuiltinExtensionFlags>(DEFAULT_BUILTIN_EXTENSION_FLAGS);

  const refresh = useCallback(async (options?: { reloadBuiltinIds?: boolean }) => {
    if (authStatus !== "authed") return;
    // extId() resolves private ids from the builtin-ids map. If the one-shot
    // bootstrap load failed (fired pre-login, or during a backend restart),
    // that map is empty and every extId(key) returns "" — so no /api/extensions
    // record matches and every private-extension flag reads false, hiding the
    // Routines/Workers tabs. Re-load it now that we're authenticated.
    if (options?.reloadBuiltinIds || !builtinIdsLoaded()) await loadBuiltinExtensionIds(5);
    try {
      const res = await fetch(`${API}/api/extensions`, { credentials: "include" });
      if (!res.ok) return;
      const payload = await res.json();
      const records = Array.isArray(payload.extensions) ? payload.extensions : [];
      const next = { ...DEFAULT_BUILTIN_EXTENSION_FLAGS };
      for (const key of BUILTIN_FLAG_KEYS) {
        const record = records.find((item: any) => item?.manifest?.id === extId(key));
        next[key] = record ? record.enabled === true : false;
      }
      setFlags(next);
    } catch {
      setFlags(DEFAULT_BUILTIN_EXTENSION_FLAGS);
    }
  }, [authStatus]);

  useEffect(() => {
    void refresh();
    const off = eventBus.subscribe("extensions_changed", () => {
      void refresh({ reloadBuiltinIds: true });
    });
    return off;
  }, [refresh]);

  return flags;
}
