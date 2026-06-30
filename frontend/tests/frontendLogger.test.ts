// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const originalConsole = {
  debug: console.debug,
  info: console.info,
  log: console.log,
  warn: console.warn,
  error: console.error,
};

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true })));
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

    expect(fetch).toHaveBeenCalled();
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

  it("does not forward benign mobile no-intent console errors", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.error({ message: "No processing needed" });
    console.error({ message: "No processing needed." });

    expect(fetch).not.toHaveBeenCalled();
  });
});
