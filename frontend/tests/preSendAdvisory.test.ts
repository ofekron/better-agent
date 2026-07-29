import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchPreSendAdvisories } from "../src/utils/preSendAdvisory";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("pre-send advisory latency budget", () => {
  it("stops waiting after 200 ms", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      })),
    );

    const result = fetchPreSendAdvisories("/api", "session", "provider", "model");
    await vi.advanceTimersByTimeAsync(199);
    let settled = false;
    void result.then(() => {
      settled = true;
    });
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    await expect(result).resolves.toEqual([]);
  });
});
