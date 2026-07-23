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
    expect(fetch).toHaveBeenCalledTimes(3);
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
    expect(fetch).toHaveBeenCalledTimes(3);
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
    expect(fetch).toHaveBeenCalledTimes(1);

    act(() => onAppStateChange?.({ isActive: true }));
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);

    rendered.unmount();
    await flush();
    expect(remove).toHaveBeenCalledTimes(1);
  });
});
