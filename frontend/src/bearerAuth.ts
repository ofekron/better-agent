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

  // One shared refresh at a time. The backend rotates the refresh token on
  // every use and revokes the family if a superseded token is replayed, so
  // two parallel 401s must NOT each fire their own /refresh — they'd race,
  // the second would present the just-rotated token, and the family would
  // self-destruct. Everyone awaits the same in-flight rotation instead.
  let refreshing: Promise<boolean> | null = null;
  const refreshOnce = (refreshUrl: string): Promise<boolean> => {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const refresh = getStoredRefreshToken();
      if (!refresh) return false;
      try {
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
      if (await refreshOnce(refreshUrl)) {
        res = await originalFetch(input, withAuth(init, src));
      }
    }
    return res;
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
