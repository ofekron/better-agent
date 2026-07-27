import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(
  path.resolve(__dirname, "../src/styles/globals.css"),
  "utf8",
);
const messageBubble = fs.readFileSync(
  path.resolve(__dirname, "../src/components/MessageBubble.tsx"),
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
      "--signal-violet",
      "--signal-mint",
      "--conversation-measure",
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
    expect(turn).toContain("max-width: var(--conversation-measure)");

    const children = ruleBodyContaining(
      ".turn-group-children",
      "position: relative",
    );
    expect(children).toContain("padding-inline-start");
    expect(children).not.toMatch(/\b(left|right)\s*:/);

    const composerChild = ruleBodyContaining(
      ".input-area > :where",
      "var(--conversation-measure)",
    );
    expect(composerChild).toContain("margin-inline: auto");
    expect(composerChild).not.toContain(".modal-overlay");
  });

  it("reserves the signature signal path for backend-owned running turns", () => {
    expect(messageBubble).toContain(
      'className={`turn-group${isRunning ? " is-running" : ""}`}',
    );

    const runningRail = ruleBodyContaining(
      ".turn-group.is-running > .turn-group-children::before",
      "var(--signal-mint)",
    );
    expect(runningRail).toContain("var(--signal-violet)");
    expect(runningRail).toContain("animation: execution-signal");

    const executionKeyframesIndex = css.indexOf("@keyframes execution-signal");
    const reducedMotionIndex = css.indexOf(
      "@media (prefers-reduced-motion: reduce)",
      executionKeyframesIndex,
    );
    const reducedMotion = css.slice(reducedMotionIndex, reducedMotionIndex + 220);
    expect(executionKeyframesIndex).toBeGreaterThan(-1);
    expect(reducedMotionIndex).toBeGreaterThan(executionKeyframesIndex);
    expect(reducedMotion).toContain(
      ".turn-group.is-running > .turn-group-children::before",
    );
    expect(reducedMotion).toContain("animation: none");
  });

  it("limits the pass to presentation declarations", () => {
    expect(ruleBodyContaining(".input-row", "var(--shadow-surface)")).toContain(
      "transition:",
    );
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });
});
