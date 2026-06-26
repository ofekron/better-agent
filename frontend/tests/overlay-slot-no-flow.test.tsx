import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const CSS = readFileSync(resolve(process.cwd(), "src/styles/globals.css"), "utf8");

vi.mock("../src/components/extensionModuleLoader", () => ({
  loadExtensionModule: vi.fn(async () => ({ Component: () => null })),
}));

import { ExtensionModuleSlot } from "../src/components/ExtensionSlots";
import type { ExtensionFrontendModule } from "../src/components/ExtensionSlots";

function ruleBody(selector: string): string {
  // Match the first `selector { ... }` block, selector at start of a rule.
  const idx = CSS.indexOf(selector);
  expect(idx, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const open = CSS.indexOf("{", idx);
  const close = CSS.indexOf("}", open);
  return CSS.slice(open + 1, close);
}

const OVERLAY_MODULE: ExtensionFrontendModule = {
  extension_id: "test.overlay-fixture",
  extension_name: "Overlay fixture",
  slot: "global-approval-overlay",
  id: "node-approvals",
  label: "Node approvals",
  kind: "module",
  module_url: "/api/extensions/test.overlay-fixture/frontend/ui/x.entry.js",
};

afterEach(() => cleanup());

describe("overlay extension-module-slot has no flow footprint", () => {
  it("base slot reserves 420px (documents the regression source)", () => {
    expect(ruleBody(".extension-module-slot {")).toMatch(/min-height:\s*420px/);
  });

  it("overlay variant neutralizes the base 420px reservation", () => {
    const body = ruleBody(".extension-module-slot--overlay {");
    // Either drops the box entirely (display:contents) or zeroes the
    // reserved height — both leave no flow space above .app when idle.
    const dropsBox = /display:\s*contents/.test(body);
    const zeroHeight = /min-height:\s*0/.test(body);
    expect(dropsBox || zeroHeight).toBe(true);
  });

  it("ExtensionModuleSlot forwards the overlay variant class onto its container", () => {
    const { container } = render(
      <ExtensionModuleSlot
        module={OVERLAY_MODULE}
        className="extension-module-slot--overlay"
      />,
    );
    const slot = container.querySelector(".extension-module-slot");
    expect(slot).not.toBeNull();
    expect(slot!.classList.contains("extension-module-slot--overlay")).toBe(true);
  });
});
