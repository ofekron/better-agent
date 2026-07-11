// Frontend-facing logical keys for builtin extensions. The REAL extension ids
// (including private/commercial ones) are fetched from
// /api/extensions/builtin-ids at bootstrap — no private id is hardcoded here.
// Public ids (ask, provider-config-sync, session-bridge, ...) may still appear
// as literals at call sites; only private ids must route through extId().

import { API } from "./api";
import { eventBus } from "./lib/eventBus";

export const BUILTIN_EXTENSION_KEYS = [
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
  "agentBoard",
  "requirements",
  "sessionBridge",
  "testape",
  "scheduler",
  "routines",
] as const;

export type BuiltinExtensionKey = (typeof BUILTIN_EXTENSION_KEYS)[number];

const _ids: Partial<Record<BuiltinExtensionKey, string>> = {};

/** Populate the id map from the backend's /api/extensions/builtin-ids. */
export function setBuiltinExtensionIds(map: Record<string, string>): void {
  for (const k of BUILTIN_EXTENSION_KEYS) {
    const id = map[k];
    if (id) _ids[k] = id;
    else delete _ids[k];
  }
}

/** Resolved extension id for a logical key, or "" if not loaded/installed. */
export function extId(key: BuiltinExtensionKey): string {
  return _ids[key] ?? "";
}

/** `${API}/api/extensions/<id>/backend` for a key — call at runtime, not at
 *  module load (the id map is populated by loadBuiltinExtensionIds). */
export function extBackendBase(key: BuiltinExtensionKey): string {
  return `${API}/api/extensions/${extId(key)}/backend`;
}

export function resolvedExtBackendBase(key: BuiltinExtensionKey): string | null {
  const id = extId(key);
  return id ? `${API}/api/extensions/${encodeURIComponent(id)}/backend` : null;
}

let _loaded = false;
export function builtinIdsLoaded(): boolean {
  return _loaded;
}

const _sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** One fetch attempt. Populates `_ids` and flips `_loaded` only on success,
 *  so callers can retry after a transient failure (pre-login 401, backend
 *  restart window). Returns true on success. */
async function _attemptLoad(): Promise<boolean> {
  try {
    const res = await fetch(`${API}/api/extensions/builtin-ids`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data || !data.ids || typeof data.ids !== "object") throw new Error("malformed builtin-ids payload");
    setBuiltinExtensionIds(data.ids as Record<string, string>);
    const wasLoaded = _loaded;
    _loaded = true;
    // Notify extId() consumers (flag gates, extBackendBase call sites) so a
    // late population re-renders them via the same channel /api/extensions
    // changes flow through. Only on the unloaded->loaded transition.
    if (!wasLoaded) eventBus.publish("extensions_changed", {});
    return true;
  } catch (err) {
    // A failure here leaves every private-extension UI unreachable (empty-id
    // URLs 404, and builtin flag gates read extId("")===""). Surface it, keep
    // `_loaded` false, and let the caller retry.
    console.error("assistant: failed to load builtin extension ids — private-extension UI will be unreachable", err);
    return false;
  }
}

/** Fetch the logical-key -> id map. `attempts > 1` retries with backoff to
 *  ride out the backend-restart window. The bootstrap caller uses the default
 *  single attempt so first paint is never blocked; useBuiltinExtensionFlags
 *  re-calls with retries once authenticated to recover a failed bootstrap. */
export async function loadBuiltinExtensionIds(attempts = 1): Promise<boolean> {
  for (let i = 0; i < attempts; i++) {
    if (await _attemptLoad()) return true;
    if (i < attempts - 1) await _sleep(Math.min(500 * 2 ** i, 4000));
  }
  return false;
}
