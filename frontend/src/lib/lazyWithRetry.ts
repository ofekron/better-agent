import { lazy } from "react";

const RELOAD_KEY = "bc_lazy_chunk_reload_at";
const RELOAD_WINDOW_MS = 30_000;
type LazyFactory = Parameters<typeof lazy>[0];
type LazyComponent = Awaited<ReturnType<LazyFactory>>["default"];

// Browsers reject a dynamic import whose URL no longer exists. This hits a
// long-lived tab after a rebuild: the already-loaded bundle still references
// the OLD content-hashed chunk, which the new build removed. index.html is
// served no-cache, so a single reload pulls the fresh module graph. The
// timestamp guard turns a genuinely-missing chunk into a surfaced error
// instead of an infinite reload loop.
function isStaleChunkError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  return (
    /Failed to fetch dynamically imported module/i.test(msg) ||
    /Importing a module script failed/i.test(msg) ||
    /error loading dynamically imported module/i.test(msg)
  );
}

export function lazyWithRetry<T extends LazyComponent>(
  factory: () => Promise<{ default: T }>,
) {
  return lazy(async () => {
    try {
      return await factory();
    } catch (err: unknown) {
      if (!isStaleChunkError(err)) throw err;
      const now = Date.now();
      const last = Number(sessionStorage.getItem(RELOAD_KEY) ?? 0);
      if (!last || now - last > RELOAD_WINDOW_MS) {
        sessionStorage.setItem(RELOAD_KEY, String(now));
        window.location.reload();
        return await new Promise<{ default: T }>(() => {});
      }
      throw err;
    }
  });
}
