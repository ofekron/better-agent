/**
 * Debug-mode gating for opt-in diagnostics that must stay OFF in normal
 * use. A "debug-mode BA instance" is one where any of these are true:
 *
 *   1. URL carries `?ba_debug=1` (or `?ba_debug=stale-view` to scope to
 *      just the stale-view detector). Sticky: the first time it is seen
 *      it is mirrored into localStorage so it survives client-side
 *      navigations that drop the query string.
 *   2. localStorage `ba_debug` is a truthy value ("1"/"true"/"stale-view").
 *   3. A Vite dev build (`import.meta.env.DEV`) — local `run.sh` dev.
 *
 * Everything here is defensive: it must never throw during module load
 * (SSR/tests can run without a DOM) and must be cheap enough to call on
 * every render.
 */

const LS_KEY = "ba_debug";

function safeLocalStorage(): Storage | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null;
    return window.localStorage;
  } catch {
    // Access can throw in privacy modes / sandboxed iframes.
    return null;
  }
}

function readUrlFlag(): string | null {
  try {
    if (typeof window === "undefined" || !window.location) return null;
    const params = new URLSearchParams(window.location.search);
    return params.get(LS_KEY);
  } catch {
    return null;
  }
}

function isTruthy(value: string | null | undefined): boolean {
  if (!value) return false;
  const v = value.trim().toLowerCase();
  return v !== "" && v !== "0" && v !== "false" && v !== "off" && v !== "no";
}

let cachedTokens: Set<string> | null = null;

/** Set of active debug tokens. `"*"` means "all debug on" (from `?ba_debug=1`
 * or `import.meta.env.DEV`); a specific token like `"stale-view"` scopes to
 * one feature. Computed once and cached — call `resetDebugFlagsCache()` in
 * tests to force a re-read. */
function debugTokens(): Set<string> {
  if (cachedTokens) return cachedTokens;
  const tokens = new Set<string>();

  const urlValue = readUrlFlag();
  if (urlValue !== null) {
    // Persist the URL intent so it survives SPA navigation that strips
    // the query string. `?ba_debug=0` explicitly turns it off (and clears
    // the sticky flag) so a debug session can be ended without a full URL
    // reset.
    const ls = safeLocalStorage();
    if (isTruthy(urlValue)) {
      if (ls) {
        try {
          ls.setItem(LS_KEY, urlValue);
        } catch {
          /* ignore quota / access errors */
        }
      }
    } else if (ls) {
      try {
        ls.removeItem(LS_KEY);
      } catch {
        /* ignore */
      }
    }
  }

  const ls = safeLocalStorage();
  const stored = ls ? ls.getItem(LS_KEY) : null;

  const raw = isTruthy(urlValue) ? urlValue : isTruthy(stored) ? stored : null;
  if (raw) {
    for (const part of raw.split(/[,\s]+/)) {
      const p = part.trim().toLowerCase();
      if (!p) continue;
      // "1"/"true"/"on" => everything on.
      if (p === "1" || p === "true" || p === "on" || p === "yes" || p === "all") {
        tokens.add("*");
      } else {
        tokens.add(p);
      }
    }
  }

  // Vite dev builds default to full debug so local development surfaces
  // mismatches without any opt-in. Never true in a production bundle.
  try {
    if (typeof import.meta !== "undefined" && import.meta.env?.DEV) {
      tokens.add("*");
    }
  } catch {
    /* import.meta may be undefined in some test transforms */
  }

  cachedTokens = tokens;
  return tokens;
}

/** True if this is a debug-mode BA instance at all. */
export function isDebugMode(): boolean {
  return debugTokens().size > 0;
}

/** True if the given debug feature token is enabled (or all-debug is on). */
export function isDebugFeature(token: string): boolean {
  const tokens = debugTokens();
  return tokens.has("*") || tokens.has(token.toLowerCase());
}

/** Force a re-read of debug flags. Test-only. */
export function resetDebugFlagsCache(): void {
  cachedTokens = null;
}
