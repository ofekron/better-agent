/**
 * Unit tests for the real `src/bearerAuth.ts` module.
 *
 * The companion `bearer-auth-install.test.ts` mocks the whole module away to
 * check that `main` wires it up; nothing exercises the actual interceptor,
 * refresh-rotate, cross-tab lock, or cookie-blocked-context logic. Auth is a
 * top security surface, so this file drives the real module: the token
 * storage wrappers (incl. private-mode fallbacks), the fetch interceptor's
 * header-injection gate, the 401 → refresh → rotate → retry path, in-tab
 * refresh serialization, the Web Locks branch + its fallback, the
 * cookie-blocked context detection, and `withTokenQuery`.
 *
 * Capacitor and the mobile sync side effect are faked; `window.fetch`,
 * `navigator.locks`, and `window.top` are swapped per test and restored.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Controllable platform/embed flags read by the mocked Capacitor + the
// locks-skip path. `vi.hoisted` so the mock factories (hoisted) can read them.
const plat = vi.hoisted(() => ({ native: false }));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => plat.native },
}));
vi.mock("../src/lib/mobileBackgroundSync", () => ({
  scheduleMobileBackgroundSyncMirror: vi.fn(),
}));

import {
  clearStoredToken,
  getStoredRefreshToken,
  getStoredToken,
  installBearerAuthInterceptor,
  setStoredToken,
  setTokens,
  withTokenQuery,
} from "../src/bearerAuth";
import { scheduleMobileBackgroundSyncMirror } from "../src/lib/mobileBackgroundSync";

const ACCESS_KEY = "better_agent_auth_token";
const REFRESH_KEY = "better_agent_refresh_token";

/** Read the Authorization header off whatever shape `init.headers` takes
 *  (the interceptor always produces a `Headers` instance; the no-op path
 *  passes the caller's init through untouched). */
function getAuthz(init: RequestInit | undefined): string | null {
  const h = init?.headers;
  if (!h) return null;
  if (h instanceof Headers) return h.get("Authorization");
  if (Array.isArray(h)) {
    const entry = h.find(([k]) => k.toLowerCase() === "authorization");
    return entry ? String(entry[1]) : null;
  }
  const obj = h as Record<string, string>;
  return obj.Authorization ?? obj.authorization ?? null;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("token storage wrappers", () => {
  beforeEach(() => {
    localStorage.clear();
    (scheduleMobileBackgroundSyncMirror as ReturnType<typeof vi.fn>).mockClear();
  });

  it("round-trips the access token and mirrors the mobile sync state", () => {
    setStoredToken("T1");
    expect(getStoredToken()).toBe("T1");
    expect(localStorage.getItem(ACCESS_KEY)).toBe("T1");
    expect(scheduleMobileBackgroundSyncMirror).toHaveBeenCalledTimes(1);

    clearStoredToken();
    expect(getStoredToken()).toBeNull();
    expect(scheduleMobileBackgroundSyncMirror).toHaveBeenCalledTimes(2);
  });

  it("stores the access + rotating refresh pair together", () => {
    setTokens("A", "R");
    expect(getStoredToken()).toBe("A");
    expect(getStoredRefreshToken()).toBe("R");
  });

  it("clearing removes both tokens", () => {
    setTokens("A", "R");
    clearStoredToken();
    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
  });

  it("returns null (not a throw) when localStorage access fails", async () => {
    // Swap in a storage whose accessors throw — simulates private mode /
    // disabled storage. Must be reinstalled before the module reads it.
    const throwStore = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    } as unknown as Storage;
    const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      value: throwStore,
      configurable: true,
      writable: true,
    });

    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    expect(() => setStoredToken("X")).not.toThrow();
    expect(() => setTokens("X", "Y")).not.toThrow();
    expect(() => clearStoredToken()).not.toThrow();

    // Restore the in-memory storage for the rest of the suite.
    if (real) Object.defineProperty(globalThis, "localStorage", real);
  });
});

describe("installBearerAuthInterceptor — header injection", () => {
  let backend: ReturnType<typeof vi.fn>;
  let realFetch: typeof window.fetch;

  beforeEach(() => {
    localStorage.clear();
    plat.native = false;
    realFetch = window.fetch;
    backend = vi.fn(async () => new Response("ok", { status: 200 }));
    window.fetch = backend as unknown as typeof window.fetch;
    installBearerAuthInterceptor();
  });
  afterEach(() => {
    window.fetch = realFetch;
  });

  it("injects Authorization: Bearer when a refresh token is present", async () => {
    setTokens("T", "R");
    await window.fetch("/api/things");

    expect(backend).toHaveBeenCalledTimes(1);
    expect(getAuthz(backend.mock.calls[0][1] as RequestInit)).toBe("Bearer T");
  });

  it("does NOT inject on a top-level browser with no refresh token (so logout holds)", async () => {
    // Access token only (cookie session), top-level web → cookie rides, no
    // bearer header that would override logout.
    setStoredToken("T");
    await window.fetch("/api/things");

    // init passed through untouched (undefined) — no Authorization injected.
    expect(backend.mock.calls[0][1]).toBeUndefined();
  });

  it("injects on a cookie-blocked context even without a refresh token", async () => {
    setStoredToken("T"); // no refresh token
    plat.native = true; // Capacitor native → cookie cannot travel
    await window.fetch("/api/things");

    expect(getAuthz(backend.mock.calls[0][1] as RequestInit)).toBe("Bearer T");
  });

  it("does not overwrite a caller-set Authorization header", async () => {
    setTokens("T", "R");
    await window.fetch("/api/things", {
      headers: { Authorization: "Caller 999" },
    });

    expect(getAuthz(backend.mock.calls[0][1] as RequestInit)).toBe(
      "Caller 999",
    );
  });

  it("is a no-op when no token is stored", async () => {
    await window.fetch("/api/things");

    const [, init] = backend.mock.calls[0];
    // init passed through untouched.
    expect(init).toBeUndefined();
  });
});

describe("installBearerAuthInterceptor — 401 refresh/rotate/retry", () => {
  let backend: ReturnType<typeof vi.fn>;
  let realFetch: typeof window.fetch;
  let mainCalls: number;

  beforeEach(() => {
    localStorage.clear();
    plat.native = false;
    realFetch = window.fetch;
    mainCalls = 0;
    backend = vi.fn();
    window.fetch = backend as unknown as typeof window.fetch;
    installBearerAuthInterceptor();
  });
  afterEach(() => {
    window.fetch = realFetch;
    delete (navigator as Navigator & { locks?: LockManager }).locks;
  });

  /** Backend that 401s the main URL once, then succeeds; refresh rotates. */
  function rotatingBackend() {
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        return json({ access_token: "T2", refresh_token: "R2" });
      }
      mainCalls++;
      return new Response(mainCalls === 1 ? "no" : "ok", {
        status: mainCalls === 1 ? 401 : 200,
      });
    });
  }

  it("rotates and retries on 401, sending the refreshed token on retry", async () => {
    setTokens("T", "R");
    rotatingBackend();

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(200);
    // main (401) → refresh → main (200): three backend calls.
    expect(backend).toHaveBeenCalledTimes(3);
    // The retried main call (3rd backend call) carries the rotated token.
    expect(getAuthz(backend.mock.calls[2][1] as RequestInit)).toBe("Bearer T2");
    expect(getStoredToken()).toBe("T2");
    expect(getStoredRefreshToken()).toBe("R2");
  });

  it("clears tokens and does not retry when refresh fails (non-ok)", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({}, 401);
      return new Response("no", { status: 401 });
    });

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(401);
    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    // main (401) → refresh (401) → no retry.
    expect(backend).toHaveBeenCalledTimes(2);
  });

  it("clears tokens when the refresh response carries no new pair", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({}); // 200, no tokens
      return new Response("no", { status: 401 });
    });

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(401);
    expect(getStoredToken()).toBeNull();
  });

  it("does not attempt refresh on the refresh call itself (no recursion)", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) =>
      json({ ok: true }),
    );

    await window.fetch("/api/auth/refresh");

    // Exactly one call — the refresh URL is not itself refreshed.
    expect(backend).toHaveBeenCalledTimes(1);
  });

  it("does not attempt refresh for a Request-object input (body may not replay)", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async () => new Response("no", { status: 401 }));

    const res = await window.fetch(new Request("/api/things"));

    expect(res.status).toBe(401);
    // One call only — Request input suppresses the retry path.
    expect(backend).toHaveBeenCalledTimes(1);
  });

  it("does not attempt refresh when there is no refresh token", async () => {
    setStoredToken("T"); // access only
    backend.mockImplementation(async () => new Response("no", { status: 401 }));

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(401);
    expect(backend).toHaveBeenCalledTimes(1);
  });

  it("serializes concurrent 401s to a single refresh in-tab", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({ access_token: "T2", refresh_token: "R2" });
      return new Response("no", { status: 401 }); // always 401
    });

    await Promise.all([window.fetch("/api/a"), window.fetch("/api/b")]);

    // Both main calls 401, but only ONE refresh call — the second 401 reuses
    // the in-flight `refreshing` promise.
    const refreshCalls = backend.mock.calls.filter((c) =>
      String(c[0]).includes("/api/auth/refresh"),
    );
    expect(refreshCalls).toHaveLength(1);
  });
});

describe("installBearerAuthInterceptor — cross-tab Web Lock", () => {
  let backend: ReturnType<typeof vi.fn>;
  let realFetch: typeof window.fetch;

  beforeEach(() => {
    localStorage.clear();
    plat.native = false;
    realFetch = window.fetch;
    backend = vi.fn();
    window.fetch = backend as unknown as typeof window.fetch;
    installBearerAuthInterceptor();
  });
  afterEach(() => {
    window.fetch = realFetch;
    delete (navigator as Navigator & { locks?: LockManager }).locks;
  });

  it("acquires the named Web Lock when rotating", async () => {
    const request = vi.fn((_name: string, fn: () => Promise<unknown>) => fn());
    Object.defineProperty(navigator, "locks", {
      value: { request },
      configurable: true,
    });
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({ access_token: "T2", refresh_token: "R2" });
      return new Response("no", { status: 401 });
    });

    await window.fetch("/api/things");

    expect(request).toHaveBeenCalledWith("better_agent_token_refresh", expect.any(Function));
  });

  it("falls back to in-tab serialization when Web Locks is unavailable", async () => {
    // No navigator.locks at all (older browser) — refresh still works.
    delete (navigator as Navigator & { locks?: LockManager }).locks;
    setTokens("T", "R");
    let mainCalls = 0;
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({ access_token: "T2", refresh_token: "R2" });
      mainCalls++;
      return new Response(mainCalls === 1 ? "no" : "ok", {
        status: mainCalls === 1 ? 401 : 200,
      });
    });

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(200);
  });

  it("reuses another tab's rotated token instead of replaying the superseded refresh", async () => {
    // While waiting for the lock, another tab rotated the access token.
    // We must reuse its result rather than POST our (now-superseded)
    // refresh token — replaying it would revoke the family.
    const request = vi.fn(async (_name: string, fn: () => Promise<unknown>) => {
      localStorage.setItem(ACCESS_KEY, "T_OTHER");
      return fn();
    });
    Object.defineProperty(navigator, "locks", {
      value: { request },
      configurable: true,
    });
    setTokens("T", "R");
    let rotated = false;
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        rotated = true; // should NEVER be reached — we short-circuit
        return json({ access_token: "X", refresh_token: "X" });
      }
      return new Response("no", { status: 401 });
    });

    await window.fetch("/api/things");

    expect(rotated).toBe(false); // no refresh POST was made
    expect(getStoredToken()).toBe("T_OTHER");
  });

  it("aborts the refresh if another tab cleared the refresh token mid-lock", async () => {
    // failedToken === current (no shortcut), but the refresh token was
    // removed while we waited — there is nothing to present.
    const request = vi.fn(async (_name: string, fn: () => Promise<unknown>) => {
      localStorage.removeItem(REFRESH_KEY);
      return fn();
    });
    Object.defineProperty(navigator, "locks", {
      value: { request },
      configurable: true,
    });
    setTokens("T", "R");
    let rotated = false;
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        rotated = true;
        return json({ access_token: "X", refresh_token: "X" });
      }
      return new Response("no", { status: 401 });
    });

    const res = await window.fetch("/api/things");

    expect(rotated).toBe(false); // no refresh token → no refresh POST
    expect(res.status).toBe(401); // no retry
  });

  it("returns false (no retry) when the refresh request itself throws", async () => {
    setTokens("T", "R");
    backend.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) throw new Error("network down");
      return new Response("no", { status: 401 });
    });

    const res = await window.fetch("/api/things");

    expect(res.status).toBe(401); // refresh threw → swallowed → no retry
  });
});

describe("withTokenQuery", () => {
  beforeEach(() => {
    localStorage.clear();
    plat.native = false;
  });

  it("appends the token as a query param in a cookie-blocked context", () => {
    plat.native = true;
    setStoredToken("a b/c"); // spaces/slash to exercise encodeURIComponent
    expect(withTokenQuery("wss://host/ws")).toBe("wss://host/ws?token=a%20b%2Fc");
  });

  it("uses & when the URL already has a query string", () => {
    plat.native = true;
    setStoredToken("T");
    expect(withTokenQuery("https://host/path?x=1")).toBe(
      "https://host/path?x=1&token=T",
    );
  });

  it("leaves the URL untouched when no token is stored", () => {
    plat.native = true;
    expect(withTokenQuery("https://host/path")).toBe("https://host/path");
  });

  it("never leaks the token into a URL on a top-level browser", () => {
    setStoredToken("T");
    expect(withTokenQuery("https://host/path")).toBe("https://host/path");
  });

  it("detects an iframe context via window.self !== window.top", () => {
    setStoredToken("T");
    const realTop = Object.getOwnPropertyDescriptor(window, "top");
    Object.defineProperty(window, "top", {
      value: { foreign: true },
      configurable: true,
    });
    try {
      expect(withTokenQuery("https://host/path")).toBe(
        "https://host/path?token=T",
      );
    } finally {
      // Restore by deleting our override so happy-dom's real value resurfaces.
      delete (window as unknown as Record<string, unknown>).top;
      if (realTop) Object.defineProperty(window, "top", realTop);
    }
  });

  it("treats a cross-origin top access (throws) as embedded", () => {
    setStoredToken("T");
    const realTop = Object.getOwnPropertyDescriptor(window, "top");
    Object.defineProperty(window, "top", {
      get() {
        throw new Error("cross-origin");
      },
      configurable: true,
    });
    try {
      expect(withTokenQuery("https://host/path")).toBe(
        "https://host/path?token=T",
      );
    } finally {
      delete (window as unknown as Record<string, unknown>).top;
      if (realTop) Object.defineProperty(window, "top", realTop);
    }
  });
});
