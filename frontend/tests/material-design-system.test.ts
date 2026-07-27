import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles/globals.css"), "utf8");

describe("Material design system", () => {
  it("defines one shared semantic foundation", () => {
    for (const token of [
      "--md-surface-container-low",
      "--md-surface-container-high",
      "--md-on-surface-variant",
      "--md-primary-container",
      "--md-outline-variant",
      "--md-control-height",
      "--md-shape-md",
      "--md-motion-standard",
    ]) {
      expect(styles).toContain(token);
    }
  });

  it("covers every major page family without changing application markup", () => {
    for (const selector of [
      ".settings-page-nav",
      ".settings-page-content",
      ".config-panel-host",
      ".analytics-stat-card",
      ".communications-panel",
      ".marketplace-bridge-status",
      ".file-viewer-header",
      ".modal-content",
    ]) {
      expect(styles).toContain(selector);
    }
  });

  it("keeps interactive states visible and respects reduced motion", () => {
    expect(styles).toContain("button:focus-visible");
    expect(styles).toContain("var(--md-state-hover)");
    expect(styles).toContain("var(--md-state-pressed)");
    expect(styles).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("keeps the conversation and composer on the shared full-width measure", () => {
    expect(styles).toContain("--conversation-measure: 100%");
    expect(styles).toContain("width: min(100%, var(--conversation-measure))");
    expect(styles).toContain("max-width: var(--conversation-measure)");
    expect(styles).toContain("padding: 24px clamp(10px, 1vw, 16px) 32px");
    expect(styles).toContain("padding-inline-start: clamp(10px, 1vw, 16px)");
    expect(styles).toContain("padding-inline-end: calc(clamp(10px, 1vw, 16px) + 8px)");
  });

  it("defines complete Nord and Dracula semantic palettes", () => {
    for (const theme of ["nord", "dracula"]) {
      const start = styles.indexOf(`:root[data-theme="${theme}"]`);
      expect(start).toBeGreaterThan(-1);
      const palette = styles.slice(start, styles.indexOf("}", start));
      for (const token of [
        "--bg-primary",
        "--text-primary",
        "--border",
        "--accent",
        "--success",
        "--error",
        "--warning",
        "--md-surface",
        "--md-primary",
        "--md-on-primary",
      ]) {
        expect(palette).toContain(`${token}:`);
      }
    }
  });

  it("aligns manager and sub-session streams with ordinary events", () => {
    const start = styles.indexOf(".manager-scope {");
    const managerScope = styles.slice(start, styles.indexOf("}", start));
    expect(managerScope).toContain("padding-inline-start: 0");
    expect(managerScope).toContain("border-inline-start: 0");
  });
});
