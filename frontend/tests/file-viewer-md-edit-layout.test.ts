import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The markdown edit view wraps Monaco in `.file-viewer-md-edit > section > div`.
// @monaco-editor/react gives that inner mount div `width:100%` but NO height, so
// without an explicit height rule Monaco's automaticLayout reads it as 5x5 and
// the editor collapses (blank / thrashing panel) when you edit a markdown file.
// The code path (`.file-viewer > section > div`) already forces height:100% for
// exactly this reason; the markdown edit path must get the same treatment.
const cssPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../src/styles/globals.css",
);
const css = readFileSync(cssPath, "utf8");

function ruleBlockFor(selector: string): string {
  const blocks = css.split("}");
  const block = blocks.find((b) => {
    const selectorText = b
      .slice(0, b.indexOf("{"))
      .replace(/\/\*[\s\S]*?\*\//g, "");
    const selectorList = selectorText.split(",");
    return selectorList.some((item) => item.trim() === selector);
  });
  if (!block) throw new Error(`no CSS rule mentions selector: ${selector}`);
  const body = block.slice(block.indexOf("{") + 1);
  return body;
}

describe("markdown edit editor fills its panel", () => {
  it("file viewer roots fill flex parents with a definite height", () => {
    const viewerBody = ruleBlockFor(".file-viewer");
    expect(viewerBody.replace(/\s/g, "")).toContain("flex:1");
    expect(viewerBody.replace(/\s/g, "")).toContain("display:flex");
    expect(viewerBody.replace(/\s/g, "")).toContain("flex-direction:column");
    expect(viewerBody.replace(/\s/g, "")).toContain("height:100%");
    expect(viewerBody.replace(/\s/g, "")).toContain("min-height:0");

    const panelsBody = ruleBlockFor(".file-panels");
    expect(panelsBody.replace(/\s/g, "")).toContain("flex:1");
    expect(panelsBody.replace(/\s/g, "")).toContain("display:flex");
    expect(panelsBody.replace(/\s/g, "")).toContain("flex-direction:column");
    expect(panelsBody.replace(/\s/g, "")).toContain("height:100%");
    expect(panelsBody.replace(/\s/g, "")).toContain("min-height:0");
  });

  it("file-edit overlay panes preserve the flex height chain", () => {
    const viewerSlotBody = ruleBlockFor(".prompt-eng-fileviewer");
    expect(viewerSlotBody.replace(/\s/g, "")).toContain("min-height:0");
    expect(viewerSlotBody.replace(/\s/g, "")).toContain("display:flex");
    expect(viewerSlotBody.replace(/\s/g, "")).toContain("flex-direction:column");
    expect(viewerSlotBody.replace(/\s/g, "")).toContain("overflow:hidden");

    const paneBody = ruleBlockFor(".multi-file-pane");
    expect(paneBody.replace(/\s/g, "")).toContain("flex:1");
    expect(paneBody.replace(/\s/g, "")).toContain("min-height:0");
    expect(paneBody.replace(/\s/g, "")).toContain("display:flex");
    expect(paneBody.replace(/\s/g, "")).toContain("flex-direction:column");
  });

  it("file panel panes preserve the flex height chain into FileViewer", () => {
    const paneBody = ruleBlockFor(".file-panels-pane");
    expect(paneBody.replace(/\s/g, "")).toContain("flex:1");
    expect(paneBody.replace(/\s/g, "")).toContain("min-height:0");
    expect(paneBody.replace(/\s/g, "")).toContain("display:flex");
    expect(paneBody.replace(/\s/g, "")).toContain("flex-direction:column");
    expect(paneBody.replace(/\s/g, "")).toContain("overflow:hidden");

    const shellBody = ruleBlockFor(".file-panels-viewer-shell");
    expect(shellBody.replace(/\s/g, "")).toContain("flex:1");
    expect(shellBody.replace(/\s/g, "")).toContain("min-height:0");
    expect(shellBody.replace(/\s/g, "")).toContain("display:flex");
    expect(shellBody.replace(/\s/g, "")).toContain("flex-direction:column");
  });

  for (const wrapper of [".file-viewer-md-edit", ".eng-file-editor-md-edit"]) {
    it(`${wrapper} > section stretches (flex + min-height:0)`, () => {
      const body = ruleBlockFor(`${wrapper} > section`);
      expect(body.replace(/\s/g, "")).toContain("flex:1");
      expect(body.replace(/\s/g, "")).toContain("min-height:0");
    });

    it(`${wrapper} > section > div has explicit height:100%`, () => {
      const body = ruleBlockFor(`${wrapper} > section > div`);
      expect(body.replace(/\s/g, "")).toContain("height:100%");
    });
  }
});
