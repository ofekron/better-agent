import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const appSource = fs.readFileSync(
  path.resolve(__dirname, "../src/App.tsx"),
  "utf8",
);
const askViewSource = fs.readFileSync(
  path.resolve(__dirname, "../../extensions/ask/ui/ask-view.entry.js"),
  "utf8",
);

describe("Ask view placement", () => {
  it("renders the Ask greeting in the chat message area, not the composer", () => {
    expect(appSource).toContain("headerNode={askDescriptionNode}");
    expect(appSource).not.toContain("composerHeaderNode={askDescriptionNode}");
  });

  it("keeps the empty Ask greeting unframed", () => {
    expect(askViewSource).not.toContain(".input-area .ask-greeting");
    expect(askViewSource).not.toMatch(/\.ask-greeting\{[^}]*border:/);
    expect(askViewSource).not.toMatch(/\.ask-greeting\{[^}]*background:/);
  });
});
