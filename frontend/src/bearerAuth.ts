// Bearer-token auth bridge for contexts where the session cookie can't
// travel.
//
// The Capacitor WebView serves the SPA from http://localhost/ but the
// backend lives at the configured server URL. Those are different origins,
// so the better_agent_session cookie (SameSite=Lax) does NOT travel on
// subsequent fetches — the user logs in, the cookie is set, the next
// /api/auth/me is cross-site, the browser drops the cookie, and the
// SPA bounces straight back to <Login />.
//
// Cross-site embeds (e.g. the TestApe Control Panel iframe) hit the same
// wall: the cookie is third-party there and never sent.
//
// Instead, after login/QR-redeem the backend hands us a bearer token
// (signed server-side, same key as the session cookie). We store it here
// and inject it as `Authorization: Bearer <token>` on every fetch. The
// interceptor is a no-op until a token is stored — top-level same-origin
// browsers keep riding the cookie.

import { Capacitor } from "@capacitor/core";

const STORAGE_KEY = "better_agent_auth_token";
const REFRESH_KEY = "better_agent_refresh_token";

export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function getStoredRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY);
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

/** Store the access + rotating refresh pair from /qr_redeem or /refresh. */
export function setTokens(access: string, refresh: string): void {
  setStoredToken(access);
  try {
    localStorage.setItem(REFRESH_KEY, refresh);
  } catch {
    /* access token alone still works until it expires */
  }
}

export function clearStoredToken(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* nothing to clear is the same outcome */
  }
}

export function installBearerAuthInterceptor(): void {
  const originalFetch = window.fetch.bind(window);

  // One shared refresh at a time, BOTH within a tab and across tabs. The
  // backend rotates the refresh token on every use and revokes the family if
  // a superseded token is replayed, so two refreshes that each present the
  // same stored token self-destruct the family. `refreshing` serializes the
  // in-tab parallel 401s; a Web Lock serializes across tabs (two tabs share
  // the same localStorage refresh token). Whoever wins the lock rotates;
  // everyone else re-reads the token the winner stored instead of replaying
  // the now-superseded one.
  let refreshing: Promise<boolean> | null = null;

  // Run `fn` while holding a cross-tab lock. Older browsers without the Web
  // Locks API fall back to in-tab serialization only (prior behavior).
  const withRefreshLock = <T>(fn: () => Promise<T>): Promise<T> => {
    const locks = (navigator as Navigator & { locks?: LockManager }).locks;
    if (locks?.request) {
      return locks.request("better_agent_token_refresh", fn) as Promise<T>;
    }
    return fn();
  };

  const refreshOnce = (refreshUrl: string, failedToken: string | null): Promise<boolean> => {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        return await withRefreshLock(async () => {
          // Another tab may have rotated while we waited for the lock. If the
          // stored access token changed since the request that 401'd, reuse
          // it rather than replaying our (now-superseded) refresh token.
          const current = getStoredToken();
          if (failedToken && current && current !== failedToken) return true;
          const refresh = getStoredRefreshToken();
          if (!refresh) return false;
          const res = await originalFetch(refreshUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ refresh_token: refresh }),
          });
          if (!res.ok) {
            clearStoredToken();
            return false;
          }
          const body = await res.json();
          if (body?.access_token && body?.refresh_token) {
            setTokens(body.access_token, body.refresh_token);
            return true;
          }
          clearStoredToken();
          return false;
        });
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  };

  const withAuth = (init?: RequestInit, src?: Request): RequestInit | undefined => {
    const token = getStoredToken();
    if (!token) return init;
    const headers = new Headers(init?.headers || src?.headers || {});
    // Respect a caller-set Authorization header (rare — we don't currently).
    if (!headers.has("authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    return { ...(init || {}), headers };
  };

  window.fetch = async (input, init) => {
    const src = input instanceof Request ? input : undefined;
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : src?.url || String(input);
    const isRefreshCall = url.includes("/api/auth/refresh");

    let res = await originalFetch(input, withAuth(init, src));

    // Access token expired? Rotate once and retry. Skip when there's no
    // refresh token (cookie-only browsers), on the refresh call itself,
    // and for Request-object inputs whose body we can't safely re-send.
    if (res.status === 401 && !isRefreshCall && !src && getStoredRefreshToken()) {
      const refreshUrl =
        new URL(url, window.location.href).origin + "/api/auth/refresh";
      const failedToken = getStoredToken();
      if (await refreshOnce(refreshUrl, failedToken)) {
        res = await originalFetch(input, withAuth(init, src));
      }
    }
    return res;
  };
}

/** True where the SameSite=Lax session cookie cannot travel: Capacitor
 * native (the WebView origin is cross-site to the backend) and
 * cross-site embeds (e.g. the TestApe Control Panel iframe). */
function isCookieBlockedContext(): boolean {
  if (Capacitor.isNativePlatform()) return true;
  try {
    return window.self !== window.top;
  } catch {
    // Cross-origin top access throws — definitely embedded.
    return true;
  }
}

/** Append `?token=<bearer>` to a URL for requests that can't send the
 * Authorization header (WS handshakes, raw <img>/<video> loads). Only in
 * cookie-blocked contexts (native / cross-site embeds) — top-level web
 * rides the session cookie and must not leak the token into URLs. */
export function withTokenQuery(url: string): string {
  if (!isCookieBlockedContext()) return url;
  const token = getStoredToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}
