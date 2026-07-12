import { logDurable } from "src/lib/frontendLogger";

const modulePromises = new Map<string, Promise<unknown>>();

function resourceTiming(url: string): Record<string, number> {
  const entry = performance.getEntriesByName(url, "resource").at(-1) as PerformanceResourceTiming | undefined;
  if (!entry) return {};
  return {
    resource_queue_ms: Math.round(entry.requestStart - entry.startTime),
    ttfb_ms: Math.round(entry.responseStart - entry.requestStart),
    download_ms: Math.round(entry.responseEnd - entry.responseStart),
    transfer_bytes: entry.transferSize,
    decoded_bytes: entry.decodedBodySize,
  };
}

export function loadExtensionModule(url: string, authScopeKey = "anonymous"): Promise<unknown> {
  const key = `${authScopeKey}\n${url}`;
  const cached = modulePromises.get(key);
  if (cached) return cached;
  const startedAt = performance.now();
  const promise = import(/* @vite-ignore */ url).then((module) => {
    const durationMs = performance.now() - startedAt;
    if (durationMs >= 50) logDurable("extensions.module", "import_eval", {
      url,
      scope: authScopeKey,
      duration_ms: Math.round(durationMs),
      cache_hit: false,
      ...resourceTiming(url),
    });
    return module;
  }).catch((error) => {
    if (modulePromises.get(key) === promise) modulePromises.delete(key);
    throw error;
  });
  modulePromises.set(key, promise);
  return promise;
}

export function disposeExtensionModules(authScopeKey: string): void {
  const prefix = `${authScopeKey}\n`;
  for (const key of modulePromises.keys()) {
    if (key.startsWith(prefix)) modulePromises.delete(key);
  }
}
