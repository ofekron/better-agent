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
  const block = blocks.find((b) => b.includes(selector));
  if (!block) throw new Error(`no CSS rule mentions selector: ${selector}`);
  const body = block.slice(block.indexOf("{") + 1);
  return body;
}

describe("markdown edit editor fills its panel", () => {
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
