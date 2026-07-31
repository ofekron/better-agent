import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles/globals.css"), "utf8");

describe("Settings mobile layout", () => {
  it("uses the single-pane Settings layout through small-tablet width", () => {
    expect(styles).toContain(
      "@media (max-width: 760px) {\n  .settings-page",
    );
    expect(styles).toContain(
      ".settings-page-layout--mobile-detail .settings-page-content {\n    display: flex;",
    );
  });

  it("keeps desktop Material spacing from overriding the mobile layout", () => {
    expect(styles).toContain(
      "@media (min-width: 761px) {\n  .settings-page-header",
    );
    expect(styles).toContain(
      ".settings-page-content {\n    padding: clamp(18px, 2.2vw, 28px);",
    );
  });

  it("keeps utility actions compact while leaving Back directly reachable", () => {
    expect(styles).toContain(
      ".settings-page-actions {\n    width: 100%;\n    display: grid;\n    grid-template-columns: auto minmax(0, 1fr);",
    );
    expect(styles).toContain(
      ".settings-page-close-action {\n    position: absolute;",
    );
    expect(styles).toContain(
      ".settings-page-mobile-action {\n    grid-column: 1 / -1;",
    );
  });

  it("stacks dense extension controls instead of overflowing", () => {
    expect(styles).toContain(
      ".extension-ui-settings-row,\n  .extension-ui-settings-group {\n    grid-template-columns: minmax(0, 1fr);",
    );
    expect(styles).toContain(
      ".extension-ui-settings-permission-main {\n    flex-direction: column;",
    );
  });
});
