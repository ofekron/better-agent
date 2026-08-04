import { describe, expect, it } from "vitest";
import {
  nextStatusFilters,
  sanitizeStatusFilters,
  statusKeysWithMode,
} from "../src/lib/sessionStatusFilters";
import { SESSION_STATUS_KEYS } from "../src/lib/sessionRegistry";

/**
 * The advanced-search status filter. One chip per status bucket carries both
 * "show only these" and "hide these", so the cycle and the derived request
 * params are the whole contract with the backend
 * (`statuses` / `exclude_statuses` on `GET /api/sessions`).
 */
describe("status filter chips", () => {
  it("cycles neutral → include → exclude → neutral", () => {
    let value = {};
    value = nextStatusFilters(value, "running");
    expect(value).toEqual({ running: "include" });
    value = nextStatusFilters(value, "running");
    expect(value).toEqual({ running: "exclude" });
    value = nextStatusFilters(value, "running");
    expect(value).toEqual({});
  });

  it("tracks each bucket independently", () => {
    const value = nextStatusFilters(nextStatusFilters({ error: "exclude" }, "idle"), "unread");
    expect(value).toEqual({ error: "exclude", idle: "include", unread: "include" });
    expect(statusKeysWithMode(value, "include")).toEqual(["unread", "idle"]);
    expect(statusKeysWithMode(value, "exclude")).toEqual(["error"]);
  });

  it("returns include/exclude keys in backend priority order", () => {
    const all = SESSION_STATUS_KEYS.reduce(
      (acc, key) => ({ ...acc, [key]: "include" as const }),
      {},
    );
    expect(statusKeysWithMode(all, "include")).toEqual([...SESSION_STATUS_KEYS]);
  });

  it("drops unknown buckets and bad modes when reading persisted filters", () => {
    expect(
      sanitizeStatusFilters({ running: "include", bogus: "include", idle: "maybe" }),
    ).toEqual({ running: "include" });
    expect(sanitizeStatusFilters(null)).toEqual({});
    expect(sanitizeStatusFilters("nope")).toEqual({});
  });
});

describe("status key vocabulary", () => {
  it("names every bucket exactly once", () => {
    expect(new Set(SESSION_STATUS_KEYS).size).toBe(SESSION_STATUS_KEYS.length);
    expect(SESSION_STATUS_KEYS).toEqual([
      "error",
      "needs_decision",
      "unread",
      "open_work",
      "running",
      "all_done",
      "idle",
    ]);
  });
});
