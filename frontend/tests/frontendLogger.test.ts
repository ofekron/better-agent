// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const capacitorMocks = vi.hoisted(() => ({
  isNativePlatform: vi.fn(() => false),
  addListener: vi.fn(() => Promise.resolve({ remove: () => {} })),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: capacitorMocks.isNativePlatform },
}));
vi.mock("@capacitor/app", () => ({
  App: { addListener: capacitorMocks.addListener },
}));

const originalConsole = {
  debug: console.debug,
  info: console.info,
  log: console.log,
  warn: console.warn,
  error: console.error,
};

function flushLoggerTransport(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true })));
  capacitorMocks.isNativePlatform.mockReturnValue(false);
  capacitorMocks.addListener.mockReset();
  capacitorMocks.addListener.mockImplementation(() => Promise.resolve({ remove: () => {} }));
  console.debug = vi.fn();
  console.info = vi.fn();
  console.log = vi.fn();
  console.warn = vi.fn();
  console.error = vi.fn();
});

afterEach(() => {
  console.debug = originalConsole.debug;
  console.info = originalConsole.info;
  console.log = originalConsole.log;
  console.warn = originalConsole.warn;
  console.error = originalConsole.error;
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
  vi.unstubAllGlobals();
});

describe("frontend logger", () => {
  it("does not forward non-error console output", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.info("TESTAPE_SDK custom_state noisy");
    console.log("normal noisy log");
    console.debug("debug noisy log");
    console.warn("warning noise");

    expect(fetch).not.toHaveBeenCalled();
  });

  it("forwards browser error events", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    window.dispatchEvent(new ErrorEvent("error", { message: "boom" }));
    await flushLoggerTransport();

    expect(fetch).toHaveBeenCalled();
  });

  it("durably forwards bounded extension performance diagnostics", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    const before = vi.mocked(fetch).mock.calls.length;
    window.dispatchEvent(new CustomEvent("better-agent:extension-performance", { detail: {
      extension: "ofek-dev.machine-nodes",
      stage: "pending.refresh_started",
      metrics: { generation: 3, trigger: "initial", subscribers: 2 },
    } }));
    await flushLoggerTransport();

    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(before);
    const payload = JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body));
    expect(payload.source).toBe("extension-perf.ofek-dev.machine-nodes");
    expect(payload.message).toContain("pending.refresh_started");
    expect(payload.message).not.toContain("authScopeKey");
  });

  it("rejects malformed or oversized extension diagnostics", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    const before = vi.mocked(fetch).mock.calls.length;
    window.dispatchEvent(new CustomEvent("better-agent:extension-performance", { detail: {
      extension: "../../secret",
      stage: "pending.refresh",
      metrics: {},
    } }));
    window.dispatchEvent(new CustomEvent("better-agent:extension-performance", { detail: {
      extension: "ofek-dev.machine-nodes",
      stage: "pending.refresh",
      metrics: { value: "x".repeat(4_097) },
    } }));
    await flushLoggerTransport();
    window.dispatchEvent(new CustomEvent("better-agent:extension-performance", { detail: {
      extension: "ofek-dev.machine-nodes",
      stage: "pending.refresh",
      metrics: { api_url: "https://private.example/api", auth_scope: "principal-secret" },
    } }));

    expect(fetch).toHaveBeenCalledTimes(before);
  });

  it("surfaces React componentStack in the stack field, not buried in the message", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    // Mirror what ErrorBoundary.componentDidCatch does: console.error with
    // an Error plus React's ErrorInfo object carrying `componentStack`.
    const err = new Error("Maximum update depth exceeded");
    const errorInfo = {
      componentStack:
        "\n    at SessionStatusBadge (/src/components/SessionStatusBadge.tsx:8:3)\n    at SessionNode (/src/components/SessionList.tsx:87:3)",
    };
    console.error("Uncaught error:", err, errorInfo);
    await flushLoggerTransport();

    expect(fetch).toHaveBeenCalledTimes(1);
    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const payload = JSON.parse(call[1].body);

    // The component tree must be in the stack field, readable.
    expect(payload.stack).toContain("React component stack:");
    expect(payload.stack).toContain("at SessionStatusBadge");
    expect(payload.stack).toContain("at SessionNode");
    // And NOT duplicated as escaped-JSON bloat inside the message.
    expect(payload.message).not.toContain("componentStack");
    expect(payload.message).not.toContain("SessionStatusBadge");
  });

  it("preserves stacks from structured console errors", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.error({
      message: "TypeError: cannot read property 'value' of null",
      stack: "TypeError: cannot read property 'value' of null\n    at flushQueue (InputArea.tsx:625:24)",
    });
    await flushLoggerTransport();

    expect(fetch).toHaveBeenCalledTimes(1);
    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const payload = JSON.parse(call[1].body);
    expect(payload.stack).toContain("at flushQueue");
  });

  it("does not forward benign mobile no-intent console errors", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.error({ message: "No processing needed" });
    console.error({ message: "No processing needed." });
    await flushLoggerTransport();

    expect(fetch).not.toHaveBeenCalled();
  });

  it("redacts credentials before durable transport", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.error(
      "download failed https://host/api?token=BEARER_SENTINEL&ticket=TICKET_SENTINEL",
      new Error("Authorization: Bearer HEADER_SENTINEL"),
    );
    await flushLoggerTransport();

    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = String(call[1].body);
    expect(body).not.toContain("BEARER_SENTINEL");
    expect(body).not.toContain("TICKET_SENTINEL");
    expect(body).not.toContain("HEADER_SENTINEL");
    expect(body).toContain("[REDACTED]");
  });

  it("logs slow timings but keeps fast timings quiet", async () => {
    const { logTiming } = await import("../src/lib/frontendLogger");
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(180);
    logTiming("load", "fast", 100, {}, 100);
    expect(fetch).not.toHaveBeenCalled();

    nowSpy.mockReturnValue(260);
    logTiming("load", "slow", 100, { items: 4 }, 100);
    await flushLoggerTransport();

    expect(fetch).toHaveBeenCalledTimes(1);
    const payload = JSON.parse(String((fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1].body));
    expect(payload.source).toBe("load");
    expect(payload.message).toContain("slow");
    expect(payload.message).toContain('"duration_ms":160');
    expect(payload.message).toContain('"items":4');
  });

  it("starts final durable transport immediately with keepalive", async () => {
    const { logDurableImmediate } = await import("../src/lib/frontendLogger");

    logDurableImmediate("websocket-traffic", "summary", { reason: "unmounted" });

    expect(fetch).toHaveBeenCalledTimes(1);
    const init = vi.mocked(fetch).mock.calls[0][1];
    expect(init?.keepalive).toBe(true);
  });

  it("logs visibilitychange and pagehide immediately, for post-hoc crash-loop forensics", async () => {
    // installFrontendLogger's DOM listeners (window/document.addEventListener)
    // accumulate across tests in this file — vi.resetModules() gives each
    // test a fresh module instance, but the real jsdom window/document
    // persist, so every prior test's listener is still attached. Assert on
    // the delta and the LAST call, matching this file's established
    // convention (see "durably forwards..." above), not an absolute count.
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    const beforeVisibility = vi.mocked(fetch).mock.calls.length;
    document.dispatchEvent(new Event("visibilitychange"));

    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(beforeVisibility);
    let payload = JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body));
    expect(payload.source).toBe("lifecycle");
    expect(payload.message).toContain("visibility_change");
    expect(payload.message).toContain('"visibility_state":"hidden"');

    const beforePagehide = vi.mocked(fetch).mock.calls.length;
    window.dispatchEvent(new Event("pagehide"));
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(beforePagehide);
    payload = JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body));
    expect(payload.source).toBe("lifecycle");
    expect(payload.message).toContain("pagehide");
  });

  it("logs Capacitor appStateChange transitions on native only", async () => {
    capacitorMocks.isNativePlatform.mockReturnValue(true);
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    expect(capacitorMocks.addListener).toHaveBeenCalledWith("appStateChange", expect.any(Function));
    const handler = capacitorMocks.addListener.mock.calls.find(
      ([event]) => event === "appStateChange",
    )?.[1];

    const before = vi.mocked(fetch).mock.calls.length;
    handler?.({ isActive: false });

    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(before);
    const payload = JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body));
    expect(payload.source).toBe("lifecycle");
    expect(payload.message).toContain("app_state_change");
    expect(payload.message).toContain('"is_active":false');
  });

  it("does not register appStateChange on the web (non-native)", async () => {
    capacitorMocks.isNativePlatform.mockReturnValue(false);
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();

    expect(capacitorMocks.addListener).not.toHaveBeenCalled();
  });

  it("heartbeats memory on native so an abrupt process kill still leaves a trail", async () => {
    // A renderer process kill (the leading crash-loop hypothesis) fires none
    // of visibilitychange/pagehide/appStateChange — the process is gone
    // before any handler runs. The heartbeat is the only sample source that
    // survives that case.
    capacitorMocks.isNativePlatform.mockReturnValue(true);
    vi.useFakeTimers();
    try {
      Object.defineProperty(performance, "memory", {
        value: { usedJSHeapSize: 111, totalJSHeapSize: 222, jsHeapSizeLimit: 333 },
        configurable: true,
      });
      const { installFrontendLogger } = await import("../src/lib/frontendLogger");
      installFrontendLogger();

      const before = vi.mocked(fetch).mock.calls.length;
      await vi.advanceTimersByTimeAsync(2_100);

      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(before);
      const payload = JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body));
      expect(payload.source).toBe("lifecycle");
      expect(payload.message).toContain("memory_heartbeat");
      expect(payload.message).toContain('"used_js_heap_bytes":111');
    } finally {
      vi.useRealTimers();
      // @ts-expect-error test-only cleanup of a non-standard perf field
      delete performance.memory;
    }
  });

  it("stops the memory heartbeat after its per-boot tick cap", async () => {
    // An unbounded interval would be a permanent per-boot network+allocation
    // beacon that could itself perturb the memory pressure under
    // investigation — it must stop on its own after a bounded window.
    capacitorMocks.isNativePlatform.mockReturnValue(true);
    vi.useFakeTimers();
    try {
      Object.defineProperty(performance, "memory", {
        value: { usedJSHeapSize: 1, totalJSHeapSize: 2, jsHeapSizeLimit: 3 },
        configurable: true,
      });
      const { installFrontendLogger } = await import("../src/lib/frontendLogger");
      installFrontendLogger();

      // 60 ticks at 2s = the documented 2-minute cap.
      await vi.advanceTimersByTimeAsync(60 * 2_000 + 100);
      const atCap = vi.mocked(fetch).mock.calls.length;

      await vi.advanceTimersByTimeAsync(10 * 2_000);
      expect(vi.mocked(fetch).mock.calls.length).toBe(atCap);
    } finally {
      vi.useRealTimers();
      // @ts-expect-error test-only cleanup of a non-standard perf field
      delete performance.memory;
    }
  });

  it("logs async failures with redaction and duration", async () => {
    const { timeAsync } = await import("../src/lib/frontendLogger");
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValueOnce(10).mockReturnValueOnce(32);

    await expect(timeAsync(
      "load",
      "bootstrap",
      () => Promise.reject(new Error("Bearer FAILURE_SENTINEL")),
    )).rejects.toThrow("FAILURE_SENTINEL");
    await flushLoggerTransport();

    const body = String((fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1].body);
    const payload = JSON.parse(body);
    expect(body).toContain("bootstrap_failed");
    expect(payload.message).toContain('"duration_ms":22');
    expect(body).not.toContain("FAILURE_SENTINEL");
    expect(body).toContain("[REDACTED]");
  });
});
