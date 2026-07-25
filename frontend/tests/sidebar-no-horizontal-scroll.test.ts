import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(
  path.resolve(__dirname, "../src/styles/globals.css"),
  "utf8",
);
const app = fs.readFileSync(path.resolve(__dirname, "../src/App.tsx"), "utf8");

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Tolerates grouped selectors (".a,\n.b {"); the lookahead stops
  // ".machine-tab" from matching ".machine-tabs".
  const match = css.match(new RegExp(`${escaped}(?![\\w-])[^{}]*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("sidebar never scrolls horizontally", () => {
  it("pins the sidebar to vertical-only scrolling", () => {
    // overflow-y alone computes overflow-x to auto, which is what turned
    // the sidebar into a horizontal scroller.
    const sidebar = ruleBody(".sidebar");
    expect(sidebar).toContain("overflow-y: auto");
    expect(sidebar).toContain("overflow-x: hidden");

    // Including every media-query override, so the mobile drawer can't
    // quietly reintroduce the scroller.
    const bodies = [...css.matchAll(/\.sidebar\s*\{([^}]*)\}/g)].map((m) => m[1]);
    for (const body of bodies) {
      expect(body).not.toMatch(/overflow(-x)?\s*:\s*(auto|scroll|visible)/);
    }
  });

  it("keeps the header measuring ghost out of the scrollable overflow", () => {
    const clip = ruleBody(".sidebar-header-ghost-clip");
    // Must be positioned, or it is not in the abspos ghost's
    // containing-block chain and the clip silently does nothing.
    expect(clip).toContain("position: absolute");
    expect(clip).toContain("overflow: hidden");
    // The clip covers the whole header row; without these it would eat
    // every click on the header buttons.
    expect(clip).toContain("visibility: hidden");
    expect(clip).toContain("pointer-events: none");

    const ghost = ruleBody(".sidebar-header-ghost");
    expect(ghost).toContain("width: max-content");
    expect(ghost).toContain("position: absolute");
    // Physical left would anchor to the wrong edge in RTL.
    expect(ghost).not.toMatch(/(^|[;{\s])left\s*:/);
    expect(ghost).toContain("inset-inline-start: 0");
  });

  it("keeps sidebar tab strips reachable instead of clipped", () => {
    // The sidebar no longer scrolls sideways, so a strip that cannot fit
    // must scroll itself or its tabs become unreachable.
    for (const strip of [".project-tabs", ".machine-tabs"]) {
      expect(ruleBody(strip), strip).toContain("overflow-x: auto");
    }
    expect(ruleBody(".machine-tab")).toContain("flex-shrink: 0");
    expect(ruleBody(".machine-tab-label")).toContain("white-space: nowrap");
  });

  it("wraps the project git actions rather than overflowing them", () => {
    expect(ruleBody(".project-git-actions")).toContain("flex-wrap: wrap");
    // A viewport-relative cap ignores the resized sidebar width.
    const commitRow = ruleBody(".project-git-commit-row");
    expect(commitRow).toContain("width: min(210px, 100%)");
    expect(commitRow).not.toMatch(/\d+vw/);
  });

  it("renders the ghost inside the clip wrapper", () => {
    const clipIndex = app.indexOf('className="sidebar-header-ghost-clip"');
    const ghostIndex = app.indexOf('className="sidebar-header-ghost"');
    expect(clipIndex).toBeGreaterThan(-1);
    expect(ghostIndex).toBeGreaterThan(clipIndex);

    const between = app.slice(clipIndex, ghostIndex);
    expect(between).not.toContain("</div>");
  });
});
