import { useCallback, useEffect, useRef, useState } from "react";
import { App as CapApp, type AppState } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";
import { logDurableImmediate } from "../lib/frontendLogger";
import { requestBackend, type BackendAccessError } from "../lib/backendAccess";

export type AuthStatus = "loading" | "anon" | "authed" | "setup" | "unreachable";

interface AuthedUser {
  username: string;
}

interface AuthGateState {
  status: AuthStatus;
  error: BackendAccessError | null;
  user: AuthedUser | null;
}

const RETRY_DELAYS_MS = [0, 1_000, 2_000, 3_000];
const REQUEST_TIMEOUT_MS = 5_000;

function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

function wait(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });
}

async function fetchWithTimeout(
  api: string,
  path: string,
  init: RequestInit,
  generationSignal: AbortSignal,
): ReturnType<typeof requestBackend> {
  const requestController = new AbortController();
  const abortRequest = () => requestController.abort(generationSignal.reason);
  generationSignal.addEventListener("abort", abortRequest, { once: true });
  const timeout = window.setTimeout(
    () => requestController.abort(new DOMException("Auth probe timed out", "TimeoutError")),
    REQUEST_TIMEOUT_MS,
  );
  try {
    return await requestBackend(api, path, init, requestController.signal);
  } finally {
    window.clearTimeout(timeout);
    generationSignal.removeEventListener("abort", abortRequest);
  }
}

async function probeAuth(api: string, signal: AbortSignal): Promise<AuthGateState> {
  const authResult = await fetchWithTimeout(
    api,
    "/api/auth/me",
    { credentials: "include" },
    signal,
  );
  if (authResult.kind === "aborted") throw authResult.error;
  if (authResult.kind === "unreachable") throw authResult.error;
  if (authResult.kind === "browser_access_blocked") {
    return { status: "unreachable", error: authResult, user: null };
  }
  const authResponse = authResult.response;
  if (authResponse.status === 200) {
    return { status: "authed", error: null, user: await authResponse.json() };
  }
  if (authResponse.status !== 401) {
    if (isRetryableStatus(authResponse.status)) throw new Error("Transient auth response");
    return {
      status: "unreachable",
      error: { kind: "http_error", scope: "auth", status: authResponse.status },
      user: null,
    };
  }

  const setupResult = await fetchWithTimeout(
    api,
    "/api/auth/needs_setup",
    { credentials: "include" },
    signal,
  );
  if (setupResult.kind === "aborted") throw setupResult.error;
  if (setupResult.kind === "unreachable") throw setupResult.error;
  if (setupResult.kind === "browser_access_blocked") {
    return { status: "unreachable", error: setupResult, user: null };
  }
  const setupResponse = setupResult.response;
  if (!setupResponse.ok) {
    if (isRetryableStatus(setupResponse.status)) throw new Error("Transient setup response");
    return {
      status: "unreachable",
      error: { kind: "http_error", scope: "setup", status: setupResponse.status },
      user: null,
    };
  }
  const setup = await setupResponse.json();
  return {
    status: setup.needs_setup ? "setup" : "anon",
    error: null,
    user: null,
  };
}

export function useAuthGate(api: string): AuthGateState & { checkAuth: () => void } {
  const [state, setState] = useState<AuthGateState>({
    status: "loading",
    error: null,
    user: null,
  });
  const activeGeneration = useRef<AbortController | null>(null);
  // Re-checks fire on every foreground/visibility/online transition (see
  // below), not just on first mount. A transient network blip during one of
  // those — common on mobile switching networks — must not blow away an
  // already-working session: it would replace live chat with a full-page
  // "unreachable" screen for a hiccup the WS layer would otherwise recover
  // from on its own (WS auto-reconnects on its own timer; the connection
  // indicator in the header reflects the interim state). Only a check that
  // never had a working session to begin with should fall through to
  // "unreachable" — enforced once, at the single point results are applied
  // (via the functional setState below, reading the latest `cur.status`
  // rather than a value captured up to ~26s earlier), so this covers every
  // path to "unreachable" (exhausted retries AND a definitive non-retryable
  // probe result), not just one of them.
  //
  // Best-effort mirror of `state.status`, read only for the `logDurableImmediate`
  // calls below — NOT the decision logic (that stays in the functional
  // setState updaters, which read the live value with no stale-capture
  // risk). A log line reading a value that's one render behind is fine;
  // the actual suppress-vs-wipe decision must not be.
  const lastLoggedStatusRef = useRef<AuthStatus>(state.status);
  useEffect(() => {
    lastLoggedStatusRef.current = state.status;
  }, [state.status]);

  const checkAuth = useCallback(() => {
    activeGeneration.current?.abort();
    const generation = new AbortController();
    activeGeneration.current = generation;
    const startedAt = Date.now();
    let attempt = 0;
    let lastError: unknown;

    void (async () => {
      for (const delay of RETRY_DELAYS_MS) {
        attempt += 1;
        try {
          if (delay > 0) await wait(delay, generation.signal);
          const result = await probeAuth(api, generation.signal);
          if (generation.signal.aborted) return;
          const suppressed = result.status === "unreachable" && lastLoggedStatusRef.current === "authed";
          // Immediate, not logDurable: a boot that resolves this and then
          // tears down (reload/backgrounded/killed) within the same tick as
          // logDurable's deferred setTimeout(0) send would lose the line
          // entirely — this is precisely the moment under investigation.
          logDurableImmediate("auth-gate", "check_resolved", {
            // The typed error preserves readable HTTP failures separately
            // from browser-blocked access and failed reachability.
            probe_result: result.status,
            probe_error: result.error,
            suppressed_unreachable: suppressed,
            attempt,
            elapsed_ms: Date.now() - startedAt,
          });
          setState((cur) => (result.status === "unreachable" && cur.status === "authed" ? cur : result));
          return;
        } catch (err) {
          lastError = err;
          if (generation.signal.aborted) return;
        }
      }
      if (generation.signal.aborted) return;
      const suppressed = lastLoggedStatusRef.current === "authed";
      const err = lastError instanceof Error ? lastError : null;
      logDurableImmediate("auth-gate", "check_exhausted", {
        // Distinguishes a stalled/blackholed network (TimeoutError, from
        // fetchWithTimeout's 5s abort) from connection-refused/DNS
        // (TypeError: Failed to fetch) from a backend that's up but
        // erroring (Error("Transient auth response") on a 5xx) — three
        // different root causes that would otherwise look identical here.
        error_name: err?.name ?? null,
        error_message: err?.message ?? null,
        suppressed_unreachable: suppressed,
        attempts: RETRY_DELAYS_MS.length,
        elapsed_ms: Date.now() - startedAt,
      });
      setState((cur) =>
        cur.status === "authed"
          ? cur
          : { status: "unreachable", error: { kind: "unreachable" }, user: null },
      );
    })();
  }, [api]);

  useEffect(() => {
    checkAuth();
    const onOnline = () => checkAuth();
    const onVisible = () => {
      if (document.visibilityState === "visible") checkAuth();
    };
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisible);

    let nativeListener: Promise<{ remove: () => Promise<void> }> | null = null;
    if (Capacitor.isNativePlatform()) {
      nativeListener = CapApp.addListener("appStateChange", (appState: AppState) => {
        if (appState.isActive) checkAuth();
      });
    }

    return () => {
      activeGeneration.current?.abort();
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisible);
      if (nativeListener) void nativeListener.then((listener) => listener.remove());
    };
  }, [checkAuth]);

  useEffect(() => {
    const onAuthUserChanged = (event: Event) => {
      const username = (event as CustomEvent).detail?.username;
      if (typeof username !== "string" || !username.trim()) return;
      setState((current) => ({ ...current, user: { username } }));
    };
    window.addEventListener("auth_user_changed", onAuthUserChanged);
    return () => window.removeEventListener("auth_user_changed", onAuthUserChanged);
  }, []);

  return { ...state, checkAuth };
}
