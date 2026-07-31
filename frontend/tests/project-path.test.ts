import { describe, expect, it } from "vitest";
import {
  canonicalProjectPath,
  projectPathName,
  sameProjectPath,
} from "../src/utils/projectPath";

describe("projectPathName", () => {
  it("uses the native separator for Windows and POSIX paths", () => {
    expect(projectPathName("C:\\Users\\Lenovo\\better-agent")).toBe("better-agent");
    expect(projectPathName("C:/Users/Lenovo/better-agent/")).toBe("better-agent");
    expect(projectPathName("/Users/ofekron/better-agent/")).toBe("better-agent");
  });

  it("returns the whole string when it contains no separator", () => {
    expect(projectPathName("standalone")).toBe("standalone");
  });

  it("returns the original text for a null/empty path", () => {
    expect(projectPathName(null)).toBe("");
    expect(projectPathName("")).toBe("");
  });
});

describe("canonicalProjectPath", () => {
  it("trims and passes POSIX paths through unchanged", () => {
    expect(canonicalProjectPath("  /Users/ofekron/app  ")).toBe("/Users/ofekron/app");
  });

  it("returns the empty string for null/undefined/blank input", () => {
    expect(canonicalProjectPath(null)).toBe("");
    expect(canonicalProjectPath(undefined)).toBe("");
    expect(canonicalProjectPath("   ")).toBe("");
  });

  it("normalizes backslashes, uppercases the drive letter, and strips trailing slashes", () => {
    expect(canonicalProjectPath("c:\\Users\\Lenovo\\app\\")).toBe("C:/Users/Lenovo/app");
  });

  it("preserves the forward-slash Windows spelling up to drive casing", () => {
    expect(canonicalProjectPath("d:/code/app/")).toBe("D:/code/app");
  });

  it("does not collapse a UNC host into a drive letter", () => {
    // A UNC path matches the Windows shape but has no drive letter at [0..1],
    // so the drive-letter uppercasing branch is skipped.
    expect(canonicalProjectPath("\\\\nas\\share\\app")).toBe("//nas/share/app");
  });
});

describe("sameProjectPath", () => {
  it("short-circuits on strict equality", () => {
    expect(sameProjectPath("/Users/ofekron/app", "/Users/ofekron/app")).toBe(true);
  });

  it("treats both Windows spellings of one directory as the same project", () => {
    expect(sameProjectPath("C:\\Users\\Lenovo\\app", "C:/Users/Lenovo/app")).toBe(true);
  });

  it("distinguishes genuinely different directories", () => {
    expect(sameProjectPath("/Users/ofekron/app", "/Users/ofekron/other")).toBe(false);
  });

  it("handles null/undefined operands", () => {
    expect(sameProjectPath(null, null)).toBe(true);
    expect(sameProjectPath(undefined, "")).toBe(true);
    expect(sameProjectPath("/a", null)).toBe(false);
  });
});
