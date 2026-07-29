import { useCallback, useEffect, useState } from "react";
import { API } from "src/api";
import { useBusEffect } from "src/hooks/useBusEffect";

const CONFIG_TOPICS = ["extension.config.settings", "extension.config"] as const;
import { trackPromise } from "src/progress/store";

/** Settings an extension contributes to the app Settings page through its
 * manifest `entrypoints.settings_sections` + a setting bound to one of them.
 * The backend owns the values; this module is a shared read cache that
 * invalidates on the extension-config WS events. */

export interface ExtensionAppSettingItem {
  extension_id: string;
  extension_name: string;
  key: string;
  label: string;
  type: "string" | "number" | "boolean";
  enum: (string | number)[];
  help: string;
  default: unknown;
  value: unknown;
}

export interface ExtensionAppSettingsSection {
  id: string;
  label: string;
  description: string;
  items: ExtensionAppSettingItem[];
}

let cache: ExtensionAppSettingsSection[] | null = null;
let inflight: Promise<ExtensionAppSettingsSection[]> | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

async function load(): Promise<ExtensionAppSettingsSection[]> {
  const res = await trackPromise("extensionAppSettings:load", () =>
    fetch(`${API}/api/extensions/app-settings`, { credentials: "include" }),
  ).promise;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = (await res.json()) as { sections?: ExtensionAppSettingsSection[] };
  return data.sections ?? [];
}

/** Fetch once and share; concurrent callers join the same request. */
export function ensureExtensionAppSettingsLoaded(): Promise<ExtensionAppSettingsSection[]> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = load()
      .then((sections) => {
        cache = sections;
        emit();
        return sections;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function refreshExtensionAppSettings(): Promise<ExtensionAppSettingsSection[]> {
  cache = null;
  return ensureExtensionAppSettingsLoaded();
}

/** Current value of one extension setting, or undefined while the cache has
 * not loaded or the extension does not declare it. */
export function extensionAppSettingValue(extensionId: string, key: string): unknown {
  if (!cache) return undefined;
  for (const section of cache) {
    for (const item of section.items) {
      if (item.extension_id === extensionId && item.key === key) return item.value;
    }
  }
  return undefined;
}

function useCacheSubscription(): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const bump = () => setTick((n) => n + 1);
    listeners.add(bump);
    void ensureExtensionAppSettingsLoaded().catch(() => {});
    return () => {
      listeners.delete(bump);
    };
  }, []);
}

/** Invalidate on any backend-side extension config change so a value edited
 * in another tab, or an extension enabled/disabled, lands here too. */
function useInvalidateOnExtensionConfig(): void {
  useBusEffect(CONFIG_TOPICS, () => void refreshExtensionAppSettings().catch(() => {}));
}

export function useExtensionAppSettings(): {
  sections: ExtensionAppSettingsSection[] | null;
  refresh: () => Promise<void>;
} {
  useCacheSubscription();
  useInvalidateOnExtensionConfig();
  const refresh = useCallback(async () => {
    await refreshExtensionAppSettings().catch(() => {});
  }, []);
  return { sections: cache, refresh };
}

/** Keeps the shared cache warm and fresh for non-rendering readers such as
 * the attention-sound gate. Mount once, app-wide. */
export function useExtensionAppSettingsSync(): void {
  useCacheSubscription();
  useInvalidateOnExtensionConfig();
}
