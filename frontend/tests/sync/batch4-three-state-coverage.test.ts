import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = (path: string) => readFileSync(`${process.cwd()}/${path}`, "utf8");

describe("batch 4 three-state coverage", () => {
  it.each([
    ["src/App.tsx", "project:touch:"],
    ["src/App.tsx", "project:add:"],
    ["src/App.tsx", "project:remove:"],
    ["src/App.tsx", "file-discussion:start:"],
    ["src/App.tsx", "file-discussion:patch:"],
    ["src/App.tsx", "file-discussion:send:"],
    ["src/App.tsx", "fileEditor:start:"],
    ["src/App.tsx", "fileEditor:cancel:"],
    ["src/App.tsx", "advSync:start:"],
    ["src/App.tsx", "file:beforeEdit:"],
    ["src/components/FileTree.tsx", "file:create:"],
    ["src/components/DirPickerModal.tsx", "directory:create:"],
    ["src/components/FileViewer.tsx", "file:save:"],
    ["src/components/FileEditor.tsx", "file-editor:save:"],
    ["src/components/FileViewer.tsx", "file:draft:${nodeId}:${path}"],
    ["src/components/FileViewer.tsx", "file:draft:delete:${reason}"],
    ["src/components/ProjectGitStatus.tsx", "project:git:"],
    ["src/components/ProjectSettings.tsx", "project:file:create:"],
  ])("routes %s %s through the canonical controller", (path, operationId) => {
    const text = source(path);
    const runner = text.indexOf("runThreeStateSync({");
    const operation = text.indexOf(operationId, Math.max(0, runner));
    expect(operation).toBeGreaterThan(-1);
    expect(text.slice(Math.max(0, operation - 500), operation + 2200)).toContain("runThreeStateSync");
  });

  it("keeps backend-owned UI selection on the explicit-ack durable backlog", () => {
    const text = source("src/utils/uiSelection.ts");
    expect(text).toContain("queueWrite({");
    expect(text).toContain('url: "/api/ui-selection"');
    expect(text).not.toContain("runThreeStateSync");
  });

  it("has no progress-only file write or raw draft deletion path", () => {
    const editor = source("src/components/FileEditor.tsx");
    const viewer = source("src/components/FileViewer.tsx");
    expect(editor).not.toContain('{ silent: true }');
    expect(viewer.match(/method: "DELETE"/g)).toHaveLength(1);
    expect(viewer.slice(viewer.indexOf('method: "DELETE"') - 900, viewer.indexOf('method: "DELETE"') + 300))
      .toContain("runThreeStateSync");
  });

  it("has no progress-only App path for the assigned editor mutations", () => {
    const app = source("src/App.tsx");
    for (const operationId of ["fileEditor:start:", "fileEditor:cancel:", "advSync:start:", "file:beforeEdit:"]) {
      const operation = app.indexOf(operationId, app.indexOf("runThreeStateSync({"));
      expect(operation).toBeGreaterThan(-1);
      expect(app.slice(Math.max(0, operation - 500), operation + 2400)).toContain("runThreeStateSync");
    }
  });
});
