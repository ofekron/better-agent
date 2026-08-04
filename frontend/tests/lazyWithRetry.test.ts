import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { lazyWithRetry } from "../src/lib/lazyWithRetry";

const RELOAD_KEY = "bc_lazy_chunk_reload_at";

/** `lazyWithRetry` wraps its factory in an async function handed to
 * React.lazy. That inner factory is the unit of behavior; pull it off the
 * lazy payload to exercise the try/catch without rendering a tree. */
function innerFactory<T>(
  lazy: ReturnType<typeof lazyWithRetry<T>>,
): () => Promise<{ default: T }> {
  const payload = (lazy as unknown as {
    _payload: { _result: () => Promise<{ default: T }> };
  })._payload;
  return payload._result;
}

const STALE_MESSAGES = [
  "Failed to fetch dynamically imported module: https://x/y.js",
  "Importing a module script failed.",
  "error loading dynamically imported module",
];

describe("lazyWithRetry", () => {
  let reloadSpy: ReturnType<typeof vi.fn>;
  let originalReload: () => void;

  beforeEach(() => {
    sessionStorage.clear();
    reloadSpy = vi.fn();
    // happy-dom's Location is a real object; redefine just the reload
    // method on it so we can observe the trigger without navigating.
    originalReload = window.location.reload.bind(window.location);
    Object.defineProperty(window.location, "reload", {
      value: reloadSpy,
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(window.location, "reload", {
      value: originalReload,
      configurable: true,
      writable: true,
    });
  });

  it("returns the factory's module on success without touching reload state", async () => {
    const module = { default: () => null };
    const lazy = lazyWithRetry(async () => module);
    await expect(innerFactory(lazy)()).resolves.toBe(module);
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(RELOAD_KEY)).toBeNull();
  });

  it("rethrows a non-stale factory error verbatim and never reloads", async () => {
    const boom = new Error("boom");
    const lazy = lazyWithRetry(async () => {
      throw boom;
    });
    await expect(innerFactory(lazy)()).rejects.toThrow("boom");
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(RELOAD_KEY)).toBeNull();
  });

  for (const msg of STALE_MESSAGES) {
    it(`treats a stale-chunk error as stale: ${msg.slice(0, 40)}`, async () => {
      const lazy = lazyWithRetry(async () => {
        throw new Error(msg);
      });
      // No prior reload timestamp → outside any window → must reload. The
      // factory rejects on a microtask, so flush before asserting the
      // reload-branch side effects.
      const p = innerFactory(lazy)();
      await new Promise((r) => setTimeout(r, 0));
      expect(reloadSpy).toHaveBeenCalledTimes(1);
      expect(sessionStorage.getItem(RELOAD_KEY)).not.toBeNull();
      // Swallow the intentionally-never-resolving promise.
      p.catch(() => {});
    });
  }

  it("reloads on the first stale error within a fresh 30s window", async () => {
    const lazy = lazyWithRetry(async () => {
      throw new Error(STALE_MESSAGES[0]);
    });
    const p = innerFactory(lazy)();
    await new Promise((r) => setTimeout(r, 0));
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    const stamp = sessionStorage.getItem(RELOAD_KEY);
    expect(stamp).not.toBeNull();
    p.catch(() => {});

    // A second stale failure within the window must NOT reload again — it
    // rethrows instead, so a genuinely broken deploy surfaces an error
    // rather than looping the reload forever.
    const lazy2 = lazyWithRetry(async () => {
      throw new Error(STALE_MESSAGES[1]);
    });
    await expect(innerFactory(lazy2)()).rejects.toThrow();
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    // Timestamp unchanged — not refreshed on the within-window rethrow.
    expect(sessionStorage.getItem(RELOAD_KEY)).toBe(stamp);
  });

  it("does not classify non-Error throwables as stale", async () => {
    // A plain string reject (no .message) is not a stale-chunk shape.
    const lazy = lazyWithRetry(async () => {
      throw "not stale";
    });
    await expect(innerFactory(lazy)()).rejects.toBe("not stale");
    expect(reloadSpy).not.toHaveBeenCalled();
  });
});
