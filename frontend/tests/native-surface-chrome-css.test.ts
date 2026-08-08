// Regression coverage for the native-surface "chrome" gaps found in
// scratchpad/ui-before-after.md (turn rhythm/connector/running-glow,
// Collapsible baseline chrome) — the CSS-source half of each fix (see
// tests/surface/chromeRestoration.test.tsx for the DOM/component half).
// Same convention as run-badge-css.test.ts/material-design-system.test.ts:
// read globals.css and assert the rule body for the native selector,
// not just that the legacy selector still exists (the legacy classes
// were always "referenced" — they just never reached the native tree).

import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(path.resolve(__dirname, "../src/styles/globals.css"), "utf8");

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("turn rhythm + connector + live glow (`.surface-turn`/`.surface-turn-body`)", () => {
  it("gives the turn wrapper the same 22px inter-turn rhythm as legacy's .turn-group", () => {
    const rule = ruleBody(".turn-group,\n.surface-turn");
    expect(rule).toContain("margin: 0 auto 22px");
  });

  it("draws the violet->mint connector line down the turn body, same as .turn-group-children::before", () => {
    const rule = ruleBody(".turn-group-children::before,\n.surface-turn-body::before");
    expect(rule).toContain("position: absolute");
    expect(rule).toContain("linear-gradient");
  });

  it("intensifies the prompt card border AND thickens/glows the connector line while `data-live=\"true\"`, same as `.turn-group.is-running`", () => {
    const promptGlow = ruleBody(
      '.turn-group.is-running > .user-message-box,\n.surface-turn[data-live="true"] > .user-message-box',
    );
    expect(promptGlow).toContain("box-shadow");

    const connectorGlow = ruleBody(
      '.turn-group.is-running > .turn-group-children::before,\n.surface-turn[data-live="true"] > .surface-turn-body::before',
    );
    expect(connectorGlow).toContain("animation: execution-signal");
  });
});

describe("Collapsible baseline chrome (`.surface-collapsible*`)", () => {
  it("gives the collapsible container real box chrome (border + background + radius), not zero-styling", () => {
    const rule = ruleBody(".timeline-block,\n.surface-collapsible");
    expect(rule).toContain("border");
    expect(rule).toContain("border-radius");
    expect(rule).toContain("background");
  });

  it("gives the toggle header a hover state and the caret its color/transition", () => {
    const header = ruleBody(".surface-collapsible-header");
    expect(header).toContain("cursor: pointer");
    expect(header).toContain("padding: 8px 12px");

    const hover = ruleBody(".timeline-toggle-header:hover,\n.surface-collapsible-header:hover");
    expect(hover).toContain("background: var(--bg-hover)");

    const arrow = ruleBody(".timeline-toggle-header .collapse-arrow,\n.surface-collapsible-header .collapse-arrow");
    expect(arrow).toContain("transition");
  });

  it("gives the expanded body its border-top separator and stacked layout", () => {
    const body = ruleBody(".timeline-block-body,\n.surface-collapsible-body");
    expect(body).toContain("border-top");
    expect(body).toContain("flex-direction: column");
  });
});
