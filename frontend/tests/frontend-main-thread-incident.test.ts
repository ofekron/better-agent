import { beforeEach, describe, expect, it, vi } from "vitest";

describe("main-thread incident bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
  });

  it("publishes exact timing only for long tasks at the diagnostic threshold", async () => {
    let callback: PerformanceObserverCallback | null = null;
    class FakeObserver {
      constructor(next: PerformanceObserverCallback) { callback = next; }
      observe = vi.fn();
    }
    vi.stubGlobal("PerformanceObserver", FakeObserver);
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const incidents: unknown[] = [];
    window.addEventListener("better-agent:performance-incident", (event) => {
      incidents.push((event as CustomEvent).detail);
    }, { once: true });

    const { installFrontendLogger } = await import("../src/lib/frontendLogger");
    installFrontendLogger();
    const entries = [
      { duration: 79, startTime: 10, entryType: "longtask", name: "self" },
      { duration: 100.4, startTime: 42.5, entryType: "longtask", name: "self" },
    ] as PerformanceEntry[];
    callback?.({ getEntries: () => entries } as PerformanceObserverEntryList, {} as PerformanceObserver);

    expect(incidents).toEqual([{ start_time: 42.5, duration_ms: 100 }]);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
