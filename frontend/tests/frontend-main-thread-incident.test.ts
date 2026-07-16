import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("main-thread incident bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("publishes exact timing only for long tasks at the diagnostic threshold", async () => {
    const observers: Array<{ callback: PerformanceObserverCallback; types: string[] }> = [];
    class FakeObserver {
      private callback: PerformanceObserverCallback;
      constructor(next: PerformanceObserverCallback) { this.callback = next; }
      observe(options: PerformanceObserverInit) {
        observers.push({ callback: this.callback, types: options.entryTypes || [] });
      }
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
    observers.find(({ types }) => types.includes("longtask"))?.callback(
      { getEntries: () => entries } as PerformanceObserverEntryList,
      {} as PerformanceObserver,
    );

    expect(incidents).toEqual([{ start_time: 42.5, duration_ms: 100 }]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("attributes long frames to bounded redacted browser script timing", async () => {
    const observers: Array<{ callback: PerformanceObserverCallback; types: string[] }> = [];
    class FakeObserver {
      private callback: PerformanceObserverCallback;
      constructor(next: PerformanceObserverCallback) { this.callback = next; }
      observe(options: PerformanceObserverInit) {
        observers.push({ callback: this.callback, types: options.entryTypes || [] });
      }
    }
    vi.stubGlobal("PerformanceObserver", FakeObserver);
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const { installFrontendLogger } = await import("../src/lib/frontendLogger");
    installFrontendLogger();
    const scripts = Array.from({ length: 7 }, (_, index) => ({
      duration: 10 + index,
      forcedStyleAndLayoutDuration: index,
      sourceURL: index === 6
        ? `${window.location.origin}/assets/index.js?token=secret`
        : "https://third.example/private/path.js?access_token=secret",
      functionName: `render/${index}`,
      invokerType: "Event Listener",
    }));
    const entry = {
      duration: 219,
      startTime: 1_000,
      entryType: "long-animation-frame",
      name: "unknown",
      blockingDuration: 169,
      renderStart: 1_180,
      styleAndLayoutStart: 1_190,
      scripts,
    } as unknown as PerformanceEntry;
    observers.find(({ types }) => types.includes("long-animation-frame"))?.callback(
      { getEntries: () => [entry] } as PerformanceObserverEntryList,
      {} as PerformanceObserver,
    );
    vi.runOnlyPendingTimers();
    await Promise.resolve();

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.message).toContain('"duration_ms":219');
    expect(body.message).toContain('"source":"/assets/index.js"');
    expect(body.message).toContain('"source":"https://third.example"');
    expect(body.message).not.toContain("secret");
    const metrics = JSON.parse(body.message.slice(body.message.indexOf("{")));
    expect(metrics.scripts).toHaveLength(5);
  });
});
