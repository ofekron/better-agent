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

function mediaBody(query: string): string {
  const start = css.indexOf(query);
  expect(start, `missing media query ${query}`).toBeGreaterThanOrEqual(0);

  const open = css.indexOf("{", start);
  expect(open, `missing media query body for ${query}`).toBeGreaterThanOrEqual(0);

  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    const char = css[index];
    if (char === "{") depth += 1;
    if (char !== "}") continue;
    depth -= 1;
    if (depth === 0) return css.slice(open + 1, index);
  }

  throw new Error(`unterminated media query ${query}`);
}

describe("InputArea prompt text metrics", () => {
  it("keeps textarea and highlight font size tied to the same responsive source", () => {
    expect(ruleBody(".input-row")).toContain(
      "--input-prompt-font-size: calc(14px * var(--app-font-scale))",
    );
    expect(ruleBody(".input-row textarea")).toContain(
      "font-size: var(--input-prompt-font-size)",
    );
    expect(ruleBody(".input-prompt-highlight")).toContain(
      "font-size: var(--input-prompt-font-size)",
    );
    expect(mediaBody("@media (max-width: 700px)")).toContain(
      "--input-prompt-font-size: calc(16px * var(--app-font-scale))",
    );
  });

  it("keeps the mobile composer from consuming the viewport", () => {
    expect(ruleBody(".input-row")).toContain("--input-prompt-max-height: 200px");
    expect(ruleBody(".input-row textarea")).toContain(
      "max-height: var(--input-prompt-max-height)",
    );
    expect(ruleBody(".input-prompt-highlight")).toContain(
      "max-height: var(--input-prompt-max-height)",
    );
    expect(mediaBody("@media (max-width: 700px)")).toContain(
      "--input-prompt-max-height: 112px",
    );
  });
});
