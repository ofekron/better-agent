import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const source = (path: string) =>
  readFileSync(resolve(import.meta.dirname, "../src", path), "utf8");

describe("harness settings loading boundaries", () => {
  it("loads Settings only when the settings route renders", () => {
    const app = source("App.tsx");
    expect(app).not.toMatch(/import\s+\{\s*SettingsPage\s*\}\s+from/);
    expect(app).toContain('import("./components/SettingsPage")');
  });

  it("loads the harness editor only after selecting Harness Profiles", () => {
    const settings = source("components/SettingsPage.tsx");
    expect(settings).not.toMatch(/import\s+\{\s*HarnessSettingsEditor\s*\}\s+from/);
    expect(settings).toContain('import("./HarnessSettingsEditor")');
  });

  it("loads FileViewer only after opening a description modal", () => {
    const editor = source("components/HarnessSettingsEditor.tsx");
    expect(editor).not.toMatch(/import\s+\{\s*FileViewer\s*\}\s+from/);
    expect(editor).toContain('import("./FileViewer")');
  });
});
