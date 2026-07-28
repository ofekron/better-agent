import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles/globals.css"), "utf8");

function rule(selector: string, fromIndex = 0): string {
  const start = styles.indexOf(`${selector} {`, fromIndex);
  expect(start).toBeGreaterThan(-1);
  return styles.slice(start, styles.indexOf("}", start));
}

describe("Material sidebar collections", () => {
  it("uses one shared card system for workers and routines", () => {
    const sharedRows = rule(".worker-row,\n.routine-row");
    expect(sharedRows).toContain("border: 1px solid var(--md-outline-variant)");
    expect(sharedRows).toContain("border-radius: var(--md-shape-sm)");
    expect(sharedRows).toContain("background: var(--md-surface-container-low)");
    expect(sharedRows).toContain("sidebar-collection-item-in");
  });

  it("keeps actions compact, accessible, and consistent", () => {
    const rowActions = rule(".worker-row-actions button,\n.routine-row-actions button");
    expect(rowActions).toContain("min-height: 32px");
    expect(rowActions).toContain("border-radius: var(--md-button-radius)");

    const runButton = rule(".routine-run-btn");
    expect(runButton).toContain("width: 32px");
    expect(runButton).toContain("height: 32px");
    expect(runButton).toContain("border-radius: var(--md-button-radius)");
  });

  it("separates Routine title, description, and facts at narrow widths", () => {
    expect(rule(".routine-row-summary")).toContain("flex-direction: column");
    expect(rule(".routine-summary-description")).toContain("-webkit-line-clamp: 2");
    expect(rule(".routine-summary-facts")).toContain("flex-wrap: wrap");
    const routineHeaderStart = styles.indexOf(".routine-row-header {");
    expect(rule(".routine-row-header", routineHeaderStart + 1)).toContain(
      "flex-wrap: wrap",
    );
  });

  it("uses semantic theme colors for statuses and errors", () => {
    expect(rule(".worker-diverged-badge")).toContain("var(--md-error-container)");
    expect(rule(".worker-needs-init-badge")).toContain("var(--warning)");
    expect(styles).toContain(
      ".routine-stopped-badge {\n  background: var(--md-error-container)",
    );
    expect(rule(".workers-panel-error,\n.routines-panel-error")).toContain(
      "var(--md-on-error-container)",
    );
  });

  it("adapts actions and dialogs for mobile and reduced motion", () => {
    expect(styles).toContain("@media (max-width: 700px)");
    expect(styles).toContain("width: calc(100vw - 24px)");
    expect(styles).toContain("min-height: 36px");
    expect(styles).toContain("@media (prefers-reduced-motion: reduce)");
    expect(styles).toContain(".worker-row,\n  .routine-row,");
  });
});
