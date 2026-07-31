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

/**
 * Regression guard for the run-badge overflowing the viewport on mobile.
 * The badge is `white-space: nowrap`, so a long label (e.g. a worker
 * description) cannot wrap. Without the truncation contract below the badge
 * grows past its flex container and — right-aligned by the footer's
 * `justify-content: flex-end` — spills off the left edge, clipping the pulse
 * dot and the start of the label. The label must truncate instead.
 */
describe("run-badge truncation (mobile out-of-bounds fix)", () => {
  it("constrains the badge stack and badge to their flex line", () => {
    expect(ruleBody(".run-badge-stack")).toContain("min-width: 0");
    expect(ruleBody(".run-badge-stack")).toContain("max-width: 100%");
    expect(ruleBody(".run-badge")).toContain("min-width: 0");
    expect(ruleBody(".run-badge")).toContain("max-width: 100%");
  });

  it("truncates the label and keeps the pulse dot and elapsed age fixed", () => {
    const label = ruleBody(".run-badge-label");
    expect(label).toContain("min-width: 0");
    expect(label).toContain("overflow: hidden");
    expect(label).toContain("text-overflow: ellipsis");

    expect(ruleBody(".run-badge-pulse")).toContain("flex: none");
    expect(ruleBody(".run-badge-age")).toContain("flex: none");
  });
});
