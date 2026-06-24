declare const __BUILD_HASH__: string;

export interface RefreshContext {
  requestId: string;
  previousHash: string;
  refreshTime: number;
}

export const REFRESH_CONTEXT_STORAGE_KEY = "bc_refresh_context";

/** Save the current build hash and request id before asking for a restart. */
export function saveRefreshContext(requestId: string) {
  localStorage.setItem(REFRESH_CONTEXT_STORAGE_KEY, JSON.stringify({
    requestId,
    previousHash: __BUILD_HASH__,
    refreshTime: Date.now(),
  }));
}

export function clearRefreshContext() {
  localStorage.removeItem(REFRESH_CONTEXT_STORAGE_KEY);
}
