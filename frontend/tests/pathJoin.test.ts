import { describe, expect, it } from "vitest";
import { joinPickerPath } from "../src/utils/pathJoin";

describe("joinPickerPath", () => {
  it("returns the trimmed child when there is no parent", () => {
    expect(joinPickerPath("", "  logs  ")).toBe("logs");
  });

  it("returns the parent when the child trims to empty", () => {
    expect(joinPickerPath("/srv/app", "   ")).toBe("/srv/app");
  });

  it("appends with a separator when the parent lacks a trailing slash", () => {
    expect(joinPickerPath("/srv/app", "logs")).toBe("/srv/app/logs");
  });

  it("does not double the separator when the parent already ends in a slash", () => {
    expect(joinPickerPath("/srv/app/", "logs")).toBe("/srv/app/logs");
  });

  it("trims whitespace around the child before joining", () => {
    expect(joinPickerPath("/srv/app", "  out  ")).toBe("/srv/app/out");
  });
});
