import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthGate } from "../src/hooks/useAuthGate";

const capacitorMocks = vi.hoisted(() => ({
  isNativePlatform: vi.fn(() => false),
  addListener: vi.fn(),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: capacitorMocks.isNativePlatform },
}));
vi.mock("@capacitor/app", () => ({
  App: { addListener: capacitorMocks.addListener },
}));

function response(status: number, body: unknown = {}): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

// checkAuth also logs each terminal outcome via a real fetch to
// /api/logs/frontend (see useAuthGate.ts) — exclude those from assertions
// that count auth-probe calls specifically.
function authFetchCallCount(): number {
  return vi.mocked(fetch).mock.calls.filter(([url]) => !String(url).includes("/api/logs/frontend")).length;
}

describe("useAuthGate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn());
    capacitorMocks.isNativePlatform.mockReturnValue(false);
    capacitorMocks.addListener.mockReset();
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("prevents an older failed generation from overwriting newer success", async () => {
    const oldRequest = deferredResponse();
    vi.mocked(fetch)
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValueOnce(response(200, { username: "ofek" }));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));

    act(() => result.current.checkAuth());
    await flush();
    expect(result.current.status).toBe("authed");

    oldRequest.resolve(response(503));
    await flush();
    await act(async () => vi.runAllTimersAsync());

    expect(result.current.status).toBe("authed");
  });

  it("retries transient HTTP failures and preserves terminal auth semantics", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(response(503))
      .mockResolvedValueOnce(response(401))
      .mockResolvedValueOnce(response(200, { needs_setup: true }));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));

    await flush();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(result.current.status).toBe("setup");
    expect(authFetchCallCount()).toBe(3);
  });

  it("starts a fresh generation when connectivity or visibility returns", async () => {
    vi.mocked(fetch).mockResolvedValue(response(200, { username: "ofek" }));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));
    await flush();

    act(() => window.dispatchEvent(new Event("online")));
    await flush();
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flush();
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flush();

    expect(result.current.status).toBe("authed");
    expect(authFetchCallCount()).toBe(3);
  });

  it("declares unreachable when a session was never established", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));

    await flush();
    await act(async () => vi.advanceTimersByTimeAsync(6_100));

    expect(result.current.status).toBe("unreachable");
    expect(result.current.error).toEqual({ kind: "unreachable" });
  });

  it("distinguishes browser-blocked access from failed reachability", async () => {
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ type: "opaque" } as Response);
    const { result } = renderHook(() => useAuthGate("https://backend.test"));

    await flush();

    expect(result.current.status).toBe("unreachable");
    expect(result.current.error).toEqual({ kind: "browser_access_blocked" });
  });

  it("does not mislabel a readable 403 as an origin rejection", async () => {
    vi.mocked(fetch).mockResolvedValue(response(403));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));

    await flush();

    expect(result.current.status).toBe("unreachable");
    expect(result.current.error).toEqual({ kind: "http_error", scope: "auth", status: 403 });
  });

  it("keeps an already-authed session instead of wiping it on a transient re-check failure", async () => {
    // A re-check fires on every foreground/visibility/online transition, not
    // just first mount. A network blip during one of those (common when a
    // phone switches networks coming back to the foreground) must not
    // replace a live, working session with the full-page "unreachable"
    // screen — that's a regression a user would read as "it crashed".
    vi.mocked(fetch).mockResolvedValueOnce(response(200, { username: "ofek" }));
    const { result } = renderHook(() => useAuthGate("https://backend.test"));
    await flush();
    expect(result.current.status).toBe("authed");

    vi.mocked(fetch).mockRejectedValue(new Error("network down"));
    act(() => window.dispatchEvent(new Event("online")));
    await act(async () => vi.advanceTimersByTimeAsync(6_100));

    expect(result.current.status).toBe("authed");
  });

  it("logs the suppression when it actually happens, so the decision is observable", async () => {
    // The one field that could silently disagree with reality (it's read
    // from a ref one render behind the live decision) — this is what's
    // worth locking, not the log payload's shape.
    vi.mocked(fetch).mockResolvedValueOnce(response(200, { username: "ofek" }));
    renderHook(() => useAuthGate("https://backend.test"));
    await flush();

    vi.mocked(fetch).mockRejectedValue(new Error("network down"));
    act(() => window.dispatchEvent(new Event("online")));
    await act(async () => vi.advanceTimersByTimeAsync(6_100));

    const logCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/api/logs/frontend"));
    const logCall = logCalls.at(-1);
    expect(logCall).toBeDefined();
    const payload = JSON.parse(String(logCall?.[1]?.body));
    expect(payload.message).toContain("check_exhausted");
    expect(payload.message).toContain('"suppressed_unreachable":true');
  });

  it("aborts in-flight work and retry timers on unmount", async () => {
    let requestSignal: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementation((_input, init) => {
      requestSignal = init?.signal as AbortSignal;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => reject(requestSignal?.reason));
      });
    });
    const { unmount } = renderHook(() => useAuthGate("https://backend.test"));
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);

    unmount();
    expect(requestSignal?.aborted).toBe(true);
    await act(async () => vi.runAllTimersAsync());

    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("times out a stalled request before retrying", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(fetch).mockImplementation((_input, init) => {
      const signal = init?.signal as AbortSignal;
      signals.push(signal);
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason));
      });
    });
    renderHook(() => useAuthGate("https://backend.test"));
    await flush();

    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(signals[0].aborted).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("checks on native foreground and removes the listener on unmount", async () => {
    capacitorMocks.isNativePlatform.mockReturnValue(true);
    const remove = vi.fn(async () => {});
    let onAppStateChange: ((state: { isActive: boolean }) => void) | undefined;
    capacitorMocks.addListener.mockImplementation(async (_event, listener) => {
      onAppStateChange = listener;
      return { remove };
    });
    vi.mocked(fetch).mockResolvedValue(response(200, { username: "ofek" }));
    const rendered = renderHook(() => useAuthGate("https://backend.test"));
    await flush();

    act(() => onAppStateChange?.({ isActive: false }));
    await flush();
    expect(authFetchCallCount()).toBe(1);

    act(() => onAppStateChange?.({ isActive: true }));
    await flush();
    expect(authFetchCallCount()).toBe(2);

    rendered.unmount();
    await flush();
    expect(remove).toHaveBeenCalledTimes(1);
  });
});
