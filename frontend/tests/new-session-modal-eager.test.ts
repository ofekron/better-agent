import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync("src/App.tsx", "utf8");

describe("NewSessionModal loading", () => {
  it("keeps session creation modal out of lazy chunk reload path", () => {
    expect(source).toContain("NewSessionModal,\n  type SessionConfig");
    expect(source).not.toContain("import(\"./components/NewSessionModal\")");
  });
});
