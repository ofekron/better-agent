import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(
  path.resolve(__dirname, "../src/styles/globals.css"),
  "utf8",
);

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("harness profile rail styles", () => {
  it("keeps selected profile names readable on the selected background", () => {
    const selected = ruleBody(".harness-profile-rail-item.is-selected");
    expect(selected).toContain("background: var(--accent-dim)");
    expect(selected).toContain("color: var(--text-primary)");
    expect(selected).not.toMatch(/(^|[;\s])color:\s*var\(--accent\)/);
  });
});
