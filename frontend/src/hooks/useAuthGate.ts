import { useCallback, useEffect, useRef, useState } from "react";
import { App as CapApp, type AppState } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";

export type AuthStatus = "loading" | "anon" | "authed" | "setup" | "unreachable";

interface AuthedUser {
  username: string;
}

interface AuthGateState {
  status: AuthStatus;
  error: string;
  user: AuthedUser | null;
}

const RETRY_DELAYS_MS = [0, 1_000, 2_000, 3_000];
const REQUEST_TIMEOUT_MS = 5_000;

function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

function unreachableError(scope: "auth" | "setup", status: number): string {
  if (status === 403) return "Backend rejected this browser origin.";
  return `Backend ${scope} probe failed with status ${status}.`;
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
  input: RequestInfo | URL,
  init: RequestInit,
  generationSignal: AbortSignal,
): Promise<Response> {
  const requestController = new AbortController();
  const abortRequest = () => requestController.abort(generationSignal.reason);
  generationSignal.addEventListener("abort", abortRequest, { once: true });
  const timeout = window.setTimeout(
    () => requestController.abort(new DOMException("Auth probe timed out", "TimeoutError")),
    REQUEST_TIMEOUT_MS,
  );
  try {
    return await fetch(input, { ...init, signal: requestController.signal });
  } finally {
    window.clearTimeout(timeout);
    generationSignal.removeEventListener("abort", abortRequest);
  }
}

async function probeAuth(api: string, signal: AbortSignal): Promise<AuthGateState> {
  const authResponse = await fetchWithTimeout(
    `${api}/api/auth/me`,
    { credentials: "include" },
    signal,
  );
  if (authResponse.status === 200) {
    return { status: "authed", error: "", user: await authResponse.json() };
  }
  if (authResponse.status !== 401) {
    if (isRetryableStatus(authResponse.status)) throw new Error("Transient auth response");
    return {
      status: "unreachable",
      error: unreachableError("auth", authResponse.status),
      user: null,
    };
  }

  const setupResponse = await fetchWithTimeout(
    `${api}/api/auth/needs_setup`,
    { credentials: "include" },
    signal,
  );
  if (!setupResponse.ok) {
    if (isRetryableStatus(setupResponse.status)) throw new Error("Transient setup response");
    return {
      status: "unreachable",
      error: unreachableError("setup", setupResponse.status),
      user: null,
    };
  }
  const setup = await setupResponse.json();
  return {
    status: setup.needs_setup ? "setup" : "anon",
    error: "",
    user: null,
  };
}

export function useAuthGate(api: string): AuthGateState & { checkAuth: () => void } {
  const [state, setState] = useState<AuthGateState>({
    status: "loading",
    error: "",
    user: null,
  });
  const activeGeneration = useRef<AbortController | null>(null);

  const checkAuth = useCallback(() => {
    activeGeneration.current?.abort();
    const generation = new AbortController();
    activeGeneration.current = generation;

    void (async () => {
      for (const delay of RETRY_DELAYS_MS) {
        try {
          if (delay > 0) await wait(delay, generation.signal);
          const result = await probeAuth(api, generation.signal);
          if (!generation.signal.aborted) setState(result);
          return;
        } catch {
          if (generation.signal.aborted) return;
        }
      }
      if (!generation.signal.aborted) {
        setState({
          status: "unreachable",
          error: "Could not reach the backend.",
          user: null,
        });
      }
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
