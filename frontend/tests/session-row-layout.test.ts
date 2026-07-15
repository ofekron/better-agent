import { describe, expect, it } from "vitest";
import { sessionRowLayoutId } from "../src/lib/sessionRowLayout";

describe("session row layout identity", () => {
  it("does not retain a selected row as a shared-layout source inside groups", () => {
    expect(sessionRowLayoutId("selected", true)).toBeUndefined();
    expect(sessionRowLayoutId("selected", false)).toBe("session-row-selected");
  });
});
