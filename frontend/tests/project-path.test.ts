import { describe, expect, it } from "vitest";
import { projectPathName } from "../src/utils/projectPath";

describe("projectPathName", () => {
  it("uses the native separator for Windows and POSIX paths", () => {
    expect(projectPathName(String.raw`C:\Users\Lenovo\better-agent`)).toBe("better-agent");
    expect(projectPathName("C:/Users/Lenovo/better-agent/")).toBe("better-agent");
    expect(projectPathName("/Users/ofekron/better-claude/")).toBe("better-claude");
  });
});
