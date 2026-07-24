// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => false },
}));

import { loadBuiltinExtensionIds } from "../src/extensionIds";

describe("loadBuiltinExtensionIds", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("times out a stalled builtin-ids fetch instead of hanging first paint forever", async () => {
    // Simulates a dead/blackholed mobile network: the request never settles
    // on its own — it only rejects if aborted. Without a wired-through
    // AbortSignal this promise (and loadBuiltinExtensionIds's) never
    // resolves, which is exactly what blocked createRoot().render() in
    // main.tsx behind loadBuiltinExtensionIds().finally(...).
    let requestSignal: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementation((_input, init) => {
      requestSignal = init?.signal as AbortSignal;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => reject(requestSignal?.reason));
      });
    });

    const loadPromise = loadBuiltinExtensionIds();
    let settled = false;
    loadPromise.then(() => {
      settled = true;
    });

    // Well before the timeout: still hanging, matching a live stalled fetch.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(settled).toBe(false);

    // Cross the timeout boundary: the fetch must be aborted and the
    // outer promise must settle (to false) instead of hanging indefinitely.
    await vi.advanceTimersByTimeAsync(3_500);
    expect(requestSignal?.aborted).toBe(true);
    await expect(loadPromise).resolves.toBe(false);
  });
});
