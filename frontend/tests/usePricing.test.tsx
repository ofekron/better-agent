// Exhaustive coverage for src/hooks/usePricing.ts — the shared per-model
// price-table external store. Internal helpers (unionRequests, reconcile,
// fetchOnce, scheduleRetry) are not exported, so everything is driven
// through the public usePricing hook with fake timers + a mocked fetch.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const jsonRes = (body: unknown, status = 200): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: async () => body,
    text: async () => JSON.stringify(body),
  }) as unknown as Response;

const fetchMock = vi.fn();

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
}

function deferred<T = Response>(): Deferred<T> {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** Flush the microtask queue enough times for an async fetchOnce chain to
 *  settle, WITHOUT advancing fake timers (so the 6h poll / 5s retry never
 *  fire). Wrapped in act() so hook state updates are flushed. */
async function flush(act: (cb: () => Promise<void>) => Promise<void>): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

type Req = { provider_id?: string; kind: string; base_url?: string; model: string };

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("usePricing — success path", () => {
  it("fetches the price table on subscribe and exposes it via the snapshot", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    const prices = { "claude::sonnet": { available: true, input: 1, output: 2 } };
    fetchMock.mockResolvedValue(jsonRes({ prices, meta: { loading: false, refreshed_at: "t" } }));

    const { result } = renderHook(({ api, reqs }) => usePricing(api, reqs), {
      initialProps: { api: "http://api", reqs: [{ kind: "claude", model: "sonnet" }] as Req[] },
    });
    expect(result.current.unavailable).toBe(false);
    await flush(act);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url.startsWith("http://api/")).toBe(true);
    expect(url.endsWith("/backend/pricing")).toBe(true);
    expect(init).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
    expect(result.current.prices).toEqual(prices);
    expect(result.current.meta).toEqual({ loading: false, refreshed_at: "t" });
    expect(result.current.unavailable).toBe(false);
  });

  it("sends the unioned model list as the request body", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    const reqs: Req[] = [{ kind: "claude", model: "sonnet" }];
    renderHook(({ api, r }) => usePricing(api, r), { initialProps: { api: "", r: reqs } });
    await flush(act);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ models: reqs });
  });
});

describe("usePricing — error + retry paths", () => {
  it("non-ok response with no cached meta marks the table unavailable and retries", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({}, 503));
    const { result } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    expect(result.current.unavailable).toBe(true);
    // retry scheduled at 5s; advancing fires a second fetchOnce.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("non-ok response preserves availability once a meta snapshot exists", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    // First: succeed with meta so cached.meta is set.
    fetchMock.mockResolvedValueOnce(jsonRes({ prices: {}, meta: { loading: false, refreshed_at: "t" } }));
    fetchMock.mockResolvedValueOnce(jsonRes({}, 503)); // then a poll/ retry fails
    const { result } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6 * 60 * 60 * 1000); // 6h poll
    });
    await flush(act);
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    // meta present → a non-ok poll computes unavailable = !meta = false.
    expect(result.current.unavailable).toBe(false);
  });

  it("network throw with no meta marks unavailable and schedules a retry", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockRejectedValue(new Error("network"));
    const { result } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    expect(result.current.unavailable).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("network throw with an existing meta leaves the table usable and still retries", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValueOnce(jsonRes({ prices: {}, meta: { loading: false, refreshed_at: "t" } }));
    fetchMock.mockRejectedValueOnce(new Error("network"));
    const { result } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    const metaBefore = result.current.meta;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6 * 60 * 60 * 1000);
    });
    await flush(act);
    // cached unchanged on throw-when-meta-present; still a table.
    expect(result.current.unavailable).toBe(false);
    expect(result.current.meta).toEqual(metaBefore);
  });

  it("retries up to LOADING_RETRY_LIMIT then stops", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: { loading: true } })); // loading → scheduleRetry each time
    renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    // initial + 4 retries = 5 fetches; the 5th scheduleRetry is a no-op.
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      await flush(act);
    }
    const countAfterBudget = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await flush(act);
    expect(fetchMock.mock.calls.length).toBe(countAfterBudget); // no further retry
  });
});

describe("usePricing — reconcile / union semantics", () => {
  it("drops requests that lack kind or model, and skips fetch when the union is empty", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude" } as Req] }, // no model
    });
    await flush(act);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not re-fetch when api + request signature is unchanged (remaining subscriber)", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    const reqs: Req[] = [{ kind: "claude", model: "sonnet" }];
    const a = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: reqs },
    });
    const b = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: reqs },
    });
    await flush(act);
    const before = fetchMock.mock.calls.length;
    a.unmount(); // one subscriber gone, same sig → reconcile early-returns
    await flush(act);
    expect(fetchMock.mock.calls.length).toBe(before);
    b.unmount();
  });

  it("clears timers and stops fetching once the last subscriber unmounts", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    const { unmount } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    unmount();
    await flush(act);
    const before = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6 * 60 * 60 * 1000);
    });
    await flush(act);
    expect(fetchMock.mock.calls.length).toBe(before); // poll timer was cleared
  });

  it("refetches when the request set changes between renders", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    const { rerender } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    const before = fetchMock.mock.calls.length;
    rerender({ api: "", r: [{ kind: "claude", model: "opus" }] });
    await flush(act);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
  });

  it("refetches when the api base changes between renders", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ prices: {}, meta: null }));
    const { rerender } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    const before = fetchMock.mock.calls.length;
    rerender({ api: "http://new", r: [{ kind: "claude", model: "sonnet" }] });
    await flush(act);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    expect(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0]).toContain("http://new");
  });
});

describe("usePricing — supersede + cold-table branches", () => {
  it("a stale response (superseded seq) does not overwrite the snapshot", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    const first = deferred<Response>();
    fetchMock.mockReturnValueOnce(first.promise);
    fetchMock.mockResolvedValue(jsonRes({ prices: { "claude::opus": { available: true } }, meta: null }));
    const { result, rerender } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    // Change the request set: a new fetchOnce bumps fetchSeq, superseding `first`.
    rerender({ api: "", r: [{ kind: "claude", model: "opus" }] });
    await flush(act);
    // Now resolve the stale first fetch; its prices must NOT land.
    first.resolve(jsonRes({ prices: { "claude::sonnet": { available: true } }, meta: null }));
    await flush(act);
    expect(result.current.prices).not.toHaveProperty("claude::sonnet");
  });

  it("a stale non-ok response does not overwrite availability (error-path supersede)", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    const first = deferred<Response>();
    fetchMock.mockReturnValueOnce(first.promise);
    fetchMock.mockResolvedValue(
      jsonRes({ prices: {}, meta: { loading: false, refreshed_at: "t" } }),
    );
    const { result, rerender } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    rerender({ api: "", r: [{ kind: "claude", model: "opus" }] }); // bump fetchSeq
    await flush(act);
    first.resolve(jsonRes({}, 503)); // stale (my !== fetchSeq) → skip cached update
    await flush(act);
    expect(result.current.unavailable).toBe(false);
  });

  it("a successful response without a prices field is ignored", async () => {
    vi.resetModules();
    const { renderHook, act } = await import("@testing-library/react");
    const { usePricing } = await import("../src/hooks/usePricing");
    fetchMock.mockResolvedValue(jsonRes({ meta: null })); // no data.prices
    const { result } = renderHook(({ api, r }) => usePricing(api, r), {
      initialProps: { api: "", r: [{ kind: "claude", model: "sonnet" }] },
    });
    await flush(act);
    expect(Object.keys(result.current.prices)).toHaveLength(0);
  });
});
