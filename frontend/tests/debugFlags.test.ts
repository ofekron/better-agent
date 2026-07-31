import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  isDebugFeature,
  isDebugMode,
  resetDebugFlagsCache,
} from "../src/lib/debugFlags";

const LS_KEY = "ba_debug";

// Snapshot the happy-dom defaults so the throwing-access tests can restore them
// and leave no clobbered global behind for the rest of the file's workers.
const originalLocalStorage = window.localStorage;
const originalLocation = window.location;

/** Point window.location.search at `search` ("" clears it). happy-dom parses
 * the href setter and updates search/query params. */
function setUrl(search: string): void {
  window.location.href = `http://localhost/${search}`;
}

beforeEach(() => {
  localStorage.clear();
  // Production-like default so the DEV branch doesn't force "*" on everywhere.
  vi.stubEnv("DEV", false);
  resetDebugFlagsCache();
});

afterEach(() => {
  vi.unstubAllEnvs();
  resetDebugFlagsCache();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: originalLocalStorage,
  });
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

describe("isDebugMode / isDebugFeature", () => {
  it("is off when no flag is present and this is not a dev build", () => {
    expect(isDebugMode()).toBe(false);
    expect(isDebugFeature("stale-view")).toBe(false);
  });

  it("turns all debug on from ?ba_debug=1 and persists the intent to localStorage", () => {
    setUrl("?ba_debug=1");
    expect(isDebugMode()).toBe(true);
    // "*" means every feature is on.
    expect(isDebugFeature("anything")).toBe(true);
    expect(localStorage.getItem(LS_KEY)).toBe("1");
  });

  it("scopes to one feature from a named ?ba_debug token", () => {
    setUrl("?ba_debug=stale-view");
    expect(isDebugFeature("stale-view")).toBe(true);
    expect(isDebugFeature("other")).toBe(false);
    expect(isDebugMode()).toBe(true);
  });

  it("keeps debug on across SPA navigation by reading the sticky localStorage value", () => {
    setUrl("?ba_debug=stale-view");
    expect(isDebugFeature("stale-view")).toBe(true);
    // Simulate a navigation that drops the query string.
    setUrl("");
    resetDebugFlagsCache();
    expect(isDebugFeature("stale-view")).toBe(true);
  });

  it("treats ?ba_debug=0 as an explicit off that clears the sticky flag", () => {
    localStorage.setItem(LS_KEY, "1");
    setUrl("?ba_debug=0");
    expect(isDebugMode()).toBe(false);
    expect(localStorage.getItem(LS_KEY)).toBe(null);
  });

  it("classifies the all-on spellings (on/yes/all/true) as the '*' token", () => {
    for (const spelling of ["on", "yes", "all", "true"]) {
      localStorage.setItem(LS_KEY, spelling);
      resetDebugFlagsCache();
      expect(isDebugFeature("anything"), `spelling=${spelling}`).toBe(true);
    }
  });

  it("treats falsy spellings (0/false/off/no) as off", () => {
    for (const spelling of ["0", "false", "off", "no"]) {
      localStorage.setItem(LS_KEY, spelling);
      resetDebugFlagsCache();
      expect(isDebugMode(), `spelling=${spelling}`).toBe(false);
    }
  });

  it("parses comma/space-separated token lists from localStorage", () => {
    localStorage.setItem(LS_KEY, "stale-view, other\tfoo");
    expect(isDebugFeature("stale-view")).toBe(true);
    expect(isDebugFeature("other")).toBe(true);
    expect(isDebugFeature("foo")).toBe(true);
    expect(isDebugFeature("missing")).toBe(false);
  });

  it("uppercases nothing — feature tokens are matched case-insensitively", () => {
    setUrl("?ba_debug=Stale-View");
    expect(isDebugFeature("STALE-VIEW")).toBe(true);
  });

  it("forces all debug on in a Vite dev build even with no other flag", () => {
    vi.stubEnv("DEV", true);
    setUrl("");
    expect(isDebugMode()).toBe(true);
    expect(isDebugFeature("anything")).toBe(true);
  });

  it("resetDebugFlagsCache forces the next read to re-evaluate env", () => {
    setUrl("");
    expect(isDebugMode()).toBe(false);
    localStorage.setItem(LS_KEY, "1");
    // Without a reset the cached empty set is returned.
    expect(isDebugMode()).toBe(false);
    resetDebugFlagsCache();
    expect(isDebugMode()).toBe(true);
  });

  it("never throws when localStorage access raises (privacy mode / sandbox)", () => {    // Replace localStorage with an accessor that throws on every property touch.
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    setUrl("?ba_debug=stale-view");
    // Still on via the URL flag; the storage failure must not propagate.
    expect(() => isDebugMode()).not.toThrow();
    expect(isDebugFeature("stale-view")).toBe(true);
  });

  it("never throws when window.location access raises (sandboxed iframe)", () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    localStorage.setItem(LS_KEY, "stale-view");
    // The URL read fails closed; the sticky localStorage value still applies.
    expect(() => isDebugMode()).not.toThrow();
    expect(isDebugFeature("stale-view")).toBe(true);
  });
});
