// Frontend-facing logical keys for builtin extensions. The REAL extension ids
// (including private/commercial ones) are fetched from
// /api/extensions/builtin-ids at bootstrap — no private id is hardcoded here.
// Public ids (ask, provider-config-sync, session-bridge, ...) may still appear
// as literals at call sites; only private ids must route through extId().

import { API } from "./api";

export const BUILTIN_EXTENSION_KEYS = [
  "ask",
  "team",
  "supervisor",
  "projectStructure",
  "machineNodes",
  "credentialBroker",
  "providerConfigSync",
  "canvas",
  "rearranger",
  "promptEngineer",
  "browserHarness",
  "agentBoard",
  "traceInspector",
  "requirements",
  "sessionBridge",
  "testape",
  "scheduler",
] as const;

export type BuiltinExtensionKey = (typeof BUILTIN_EXTENSION_KEYS)[number];

const _ids: Partial<Record<BuiltinExtensionKey, string>> = {};

/** Populate the id map from the backend's /api/extensions/builtin-ids. */
export function setBuiltinExtensionIds(map: Record<string, string>): void {
  for (const k of BUILTIN_EXTENSION_KEYS) {
    if (map[k]) _ids[k] = map[k];
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

let _loaded = false;
export function builtinIdsLoaded(): boolean {
  return _loaded;
}

/** Fetch the logical-key -> id map once at app bootstrap (before render). */
export async function loadBuiltinExtensionIds(): Promise<void> {
  try {
    const res = await fetch(`${API}/api/extensions/builtin-ids`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data && typeof data.ids === "object") setBuiltinExtensionIds(data.ids as Record<string, string>);
  } catch (err) {
    // best-effort: features whose id didn't load build empty-id URLs and 404.
    // Surface the failure so it isn't silently mistaken for "no extensions".
    console.error("assistant: failed to load builtin extension ids — private-extension UI will be unreachable", err);
  }
  _loaded = true;
}
