import { afterEach, describe, expect, it, vi } from "vitest";
import { uuidv4 } from "../src/lib/uuid";

const CANONICAL_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("uuidv4", () => {
  it("uses native crypto.randomUUID when available (secure context)", () => {
    const native = vi.fn(() => "11111111-1111-4111-8111-111111111111");
    vi.stubGlobal("crypto", { randomUUID: native, getRandomValues: () => {} });
    expect(uuidv4()).toBe("11111111-1111-4111-8111-111111111111");
    expect(native).toHaveBeenCalledOnce();
  });

  it("falls back to a canonical v4 UUID in a non-secure context (no randomUUID)", () => {
    // Simulate plain-http LAN: randomUUID is undefined, getRandomValues exists.
    vi.stubGlobal("crypto", {
      randomUUID: undefined,
      getRandomValues: (arr: Uint8Array) => {
        for (let i = 0; i < arr.length; i++) arr[i] = (i * 37 + 5) & 0xff;
        return arr;
      },
    });
    const id = uuidv4();
    expect(id).toMatch(CANONICAL_V4);
    expect(id).toBe(id.toLowerCase());
  });

  it("does not throw when randomUUID is missing", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (arr: Uint8Array) => arr,
    });
    expect(() => uuidv4()).not.toThrow();
    expect(uuidv4()).toMatch(CANONICAL_V4);
  });
});
