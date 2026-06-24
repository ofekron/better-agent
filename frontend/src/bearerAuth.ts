// Capacitor-native auth bridge.
//
// The Capacitor WebView serves the SPA from http://localhost/ but the
// backend lives at http://<lan-ip>:8000. Those are different origins,
// so the better_agent_session cookie (SameSite=Lax) does NOT travel on
// subsequent fetches — the user logs in, the cookie is set, the next
// /api/auth/me is cross-site, the browser drops the cookie, and the
// SPA bounces straight back to <Login />.
//
// Instead, after login the backend hands us a bearer token (signed
// server-side, same key as the session cookie). We store it here and
// inject it as `Authorization: Bearer <token>` on every fetch.
//
// Desktop / same-origin browsers never call this — the cookie path
// still works there.

const STORAGE_KEY = "better_agent_auth_token";

export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredToken(token: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, token);
  } catch {
    /* private mode etc. — token-less requests will 401 and the user
     * bounces back to login, which is the right fallback. */
  }
}

export function clearStoredToken(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* nothing to clear is the same outcome */
  }
}

export function installBearerAuthInterceptor(): void {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const token = getStoredToken();
    if (!token) return originalFetch(input, init);
    const headers = new Headers(
      init?.headers ||
        (input instanceof Request ? input.headers : undefined) ||
        {},
    );
    // If the caller already set their own Authorization header (rare —
    // we don't currently), respect it.
    if (!headers.has("authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    const nextInit: RequestInit = { ...(init || {}), headers };
    return originalFetch(input, nextInit);
  };
}

/** Append `?token=<bearer>` to a WebSocket URL on native — browsers
 * don't let JS set Authorization on the WS handshake, so we ship the
 * token as a query param. Backend accepts it the same way it accepts
 * the Bearer header. */
export function withTokenQuery(url: string): string {
  const token = getStoredToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}
