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
  it("does not forward console output", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    console.info("TESTAPE_SDK custom_state noisy");
    console.log("normal noisy log");
    console.debug("debug noisy log");
    console.warn("warning noise");
    console.error("error noise");

    expect(fetch).not.toHaveBeenCalled();
  });

  it("forwards browser error events", async () => {
    const { installFrontendLogger } = await import("../src/lib/frontendLogger");

    installFrontendLogger();
    window.dispatchEvent(new ErrorEvent("error", { message: "boom" }));

    expect(fetch).toHaveBeenCalled();
  });
});
