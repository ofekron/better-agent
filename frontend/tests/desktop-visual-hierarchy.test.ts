import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(
  path.resolve(__dirname, "../src/styles/globals.css"),
  "utf8",
);

function ruleBodies(selector: string): string[] {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [...css.matchAll(new RegExp(`${escaped}(?![\\w-])[^{}]*\\{([^}]*)\\}`, "g"))]
    .map((match) => match[1]);
}

function ruleBodyContaining(selector: string, value: string): string {
  const body = ruleBodies(selector).find((candidate) => candidate.includes(value));
  expect(body, `missing ${selector} rule containing ${value}`).toBeDefined();
  return body!;
}

describe("desktop visual hierarchy", () => {
  it("uses one shared surface system across the workspace", () => {
    for (const token of [
      "--bg-canvas",
      "--bg-surface",
      "--bg-surface-raised",
      "--border-subtle",
      "--border-strong",
      "--shadow-surface",
    ]) {
      expect(css).toContain(`${token}:`);
    }

    expect(ruleBodyContaining(".sidebar", "var(--bg-surface)")).toContain(
      "var(--border-subtle)",
    );
    expect(ruleBodyContaining(".chat-container", "var(--bg-canvas)")).toContain(
      "background: var(--bg-canvas)",
    );
    expect(ruleBodyContaining(".input-row", "var(--bg-surface-raised)")).toContain(
      "var(--shadow-surface)",
    );
  });

  it("makes active navigation visibly distinct without changing its structure", () => {
    const activeSession = ruleBodyContaining(
      ".session-tab-wrapper.active",
      "var(--bg-canvas)",
    );
    expect(activeSession).toContain("var(--accent)");

    const activeSidebar = ruleBodyContaining(
      ".sidebar-tab.active",
      "var(--bg-surface-raised)",
    );
    expect(activeSidebar).toContain("var(--purple-80)");
  });

  it("keeps the conversation readable as grouped content", () => {
    const turn = ruleBodyContaining(".turn-group", "max-width");
    expect(turn).toContain("max-width: 1480px");

    const children = ruleBodyContaining(
      ".turn-group-children",
      "border-inline-start",
    );
    expect(children).toContain("padding-inline-start");
    expect(children).not.toMatch(/\b(left|right)\s*:/);
  });

  it("limits the pass to presentation declarations", () => {
    expect(ruleBodyContaining(".input-row", "var(--shadow-surface)")).toContain(
      "transition:",
    );
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });
});
