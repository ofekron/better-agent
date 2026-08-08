import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readFlag } from "../src/adapter/flag";

const STORAGE_KEY = "ba.surface_v2";

const originalLocalStorage = window.localStorage;
const originalLocation = window.location;

/** Point window.location.search at `search` ("" clears it). happy-dom parses
 * the href setter and updates search/query params. */
function setUrl(search: string): void {
  window.location.href = `http://localhost/${search}`;
}

beforeEach(() => {
  localStorage.clear();
  setUrl("");
});

afterEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: originalLocalStorage,
  });
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

describe("readFlag", () => {
  it("is ON by default — no query param, no localStorage entry", () => {
    expect(readFlag()).toBe(true);
  });

  it("is ON when localStorage holds anything other than the explicit '0' opt-out", () => {
    localStorage.setItem(STORAGE_KEY, "1");
    expect(readFlag()).toBe(true);
  });

  it("is OFF when localStorage holds the explicit '0' opt-out kill-switch", () => {
    localStorage.setItem(STORAGE_KEY, "0");
    expect(readFlag()).toBe(false);
  });

  it("is OFF when ?surface_v2=0 is present, regardless of localStorage", () => {
    localStorage.setItem(STORAGE_KEY, "1");
    setUrl("?surface_v2=0");
    expect(readFlag()).toBe(false);
  });

  it("is forced ON by ?surface_v2=1 even when localStorage opts out", () => {
    localStorage.setItem(STORAGE_KEY, "0");
    setUrl("?surface_v2=1");
    expect(readFlag()).toBe(true);
  });

  it("falls through to the localStorage check when the query param is absent", () => {
    localStorage.setItem(STORAGE_KEY, "0");
    setUrl("?other=param");
    expect(readFlag()).toBe(false);
  });

  it("never throws when localStorage access raises (privacy mode / sandbox) — defaults ON", () => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    expect(() => readFlag()).not.toThrow();
    expect(readFlag()).toBe(true);
  });

  it("never throws when window.location access raises (sandboxed iframe) — falls through to storage", () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    localStorage.setItem(STORAGE_KEY, "0");
    expect(() => readFlag()).not.toThrow();
    expect(readFlag()).toBe(false);
  });
});
