import { describe, expect, it } from "vitest";
import { statusGroupI18nKey } from "../src/lib/sessionStatusGroups";

describe("session status group labels", () => {
  it("distinguishes truly new sessions from inactive sessions", () => {
    expect(statusGroupI18nKey(7)).toBe("session.statusGroup.new");
    expect(statusGroupI18nKey(0)).toBe("session.statusGroup.inactive");
  });
});
