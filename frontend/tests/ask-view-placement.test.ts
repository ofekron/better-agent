import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const appSource = fs.readFileSync(
  path.resolve(__dirname, "../src/App.tsx"),
  "utf8",
);
describe("Ask view placement", () => {
  it("renders the Ask greeting in the chat message area, not the composer", () => {
    expect(appSource).toContain("const headerNode = askDescriptionNode || undefined");
    expect(appSource).toContain("headerNode={headerNode}");
    expect(appSource).not.toContain("composerHeaderNode={askDescriptionNode}");
  });
});
