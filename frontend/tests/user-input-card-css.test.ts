import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "../src/styles/globals.css"), "utf8");

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("UserInputCard batch layout", () => {
  it("keeps batched questions inside a scrollable card body", () => {
    const body = ruleBody(".user-input-card__questions");
    expect(body).toContain("display: grid");
    expect(body).toContain("max-height: min(52vh, 620px)");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("overscroll-behavior: contain");
  });
});

describe("Timeline entity alignment", () => {
  it("insets flattened worker and sub-session panels to match primary events", () => {
    const body = ruleBody(".timeline-flattened-entity-block");
    expect(body).toContain("padding-inline: 14px");
  });
});
