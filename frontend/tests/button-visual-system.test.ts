import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles/globals.css"), "utf8");

function rule(selector: string): string {
  const start = styles.indexOf(`${selector} {`);
  expect(start).toBeGreaterThan(-1);
  return styles.slice(start, styles.indexOf("}", start));
}

describe("button visual hierarchy", () => {
  it("uses one compact button shape while retaining 44px composer targets", () => {
    expect(styles).toContain("--md-button-radius: 8px");
    expect(rule(".input-row")).toContain("--input-action-height: 44px");
    expect(rule(".send-btn")).toContain("border-radius: var(--md-button-radius)");
    expect(rule(".input-row .stop-btn")).toContain("border-radius: var(--md-button-radius)");
    expect(rule(".composer-focus-btn")).toContain("border-radius: var(--md-button-radius)");
    expect(rule(".input-overflow-trigger")).toContain("border-radius: var(--md-button-radius)");
    expect(styles).toContain("--input-action-height: 40px;\n    gap: 4px;\n    padding: 5px;\n    border-radius: 12px");
  });

  it("keeps one primary composer action and lowers secondary emphasis", () => {
    expect(rule(".send-btn")).toContain("background: var(--md-primary)");
    expect(rule(".send-btn.queue")).toContain("background: transparent");
    expect(rule(".send-btn.queue")).toContain("border-color: var(--md-outline)");
    expect(rule(".send-btn.interrupt")).toContain("var(--warning)");
    expect(rule(".send-btn.interrupt")).not.toContain("var(--error)");
  });

  it("uses the shared compact shape across general action buttons", () => {
    const sharedStart = styles.indexOf(".setup-cancel-btn,\n.setup-save-btn,");
    const sharedRule = styles.slice(sharedStart, styles.indexOf("}", sharedStart));
    for (const selector of [".btn-primary", ".btn-secondary", ".btn-warning"]) {
      expect(sharedRule).toContain(selector);
    }
    expect(sharedRule).toContain("border-radius: var(--md-button-radius)");
    expect(rule(".settings-page-nav button,\n.harness-profile-rail-item")).toContain(
      "border-radius: var(--md-button-radius)",
    );
  });

  it("renders tool output inside one card without a nested box", () => {
    expect(rule(".tool-call")).toContain("border: 1px solid var(--border-subtle)");
    expect(rule(".bash-result-content")).toContain("background: transparent");
    expect(rule(".bash-result-content")).toContain("border: 0");
    expect(rule(".tool-result-content")).toContain("background: transparent");
    expect(rule(".tool-result-content")).toContain("border: none");
  });
});
