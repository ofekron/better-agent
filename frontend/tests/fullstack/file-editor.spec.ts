import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import { addProjectByPath } from "./harness/projects";
import { createSessionWithPrompt } from "./harness/session";

// Validates the real file browser + Monaco editor + save round-trip:
// frontend/src/components/FileTree.tsx (tree row click) →
// frontend/src/components/FilePanels.tsx / FileViewer.tsx (Monaco editor,
// autosaved draft, explicit save) → real `POST /api/file`
// (backend/file_browser.py) writing the REAL file on disk. No mocks — the
// final assertion reads the file back with Node's `fs`, not the DOM.
//
// A freshly created session's `cwd` defaults to the backend process's real
// `Path.home()` (see `session_store.create_session`: `cwd or
// str(Path.home())`), NOT the isolated `BETTER_AGENT_HOME` — so the file
// tree must be pointed at a controlled directory explicitly. We do this by
// registering our own temp directory as a project (the real "+" →
// DirPickerModal → `POST /api/projects` flow, via the existing
// `addProjectByPath` harness helper) BEFORE creating the session:
// `handleAddProject` sets App.tsx's `cwd`/`selectedProjectPath` state
// synchronously, which becomes `NewSessionModal`'s `defaultCwd`, which in
// turn becomes the new session's `cwd` — so the file tree opened afterwards
// browses exactly this directory.
test("edits a real file through Monaco and saves it to disk", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace");
  mkdirSync(workspaceDir, { recursive: true });
  const filePath = path.join(workspaceDir, "notes.txt");
  const originalContent = "original content";
  writeFileSync(filePath, originalContent);

  await addProjectByPath(page, workspaceDir);

  await createSessionWithPrompt(page, "Reply with exactly the single word: OK.");

  await page.getByTitle("Browse project files").click();
  const chooser = page.locator(".file-chooser-content");
  await expect(chooser).toBeVisible();

  const fileRow = chooser.locator(".tree-node.tree-file", { hasText: "notes.txt" });
  await fileRow.click();
  await expect(chooser).not.toBeVisible();

  const fileViewer = page.locator(".file-viewer");
  await expect(fileViewer).toBeVisible();
  await expect(fileViewer.locator(".file-viewer-path")).toContainText("notes.txt");

  const editor = fileViewer.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });
  await expect(editor.locator(".view-lines")).toContainText(originalContent, { timeout: 15_000 });

  const updatedContent = "updated by the fullstack file-editor test";
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(updatedContent);

  await expect(fileViewer.locator(".file-viewer-dirty")).toBeVisible();

  const saveButton = fileViewer.locator('button[title="Save (Cmd+S)"]');
  await expect(saveButton).toBeEnabled();
  await saveButton.click();

  await expect(fileViewer.locator(".file-viewer-dirty")).not.toBeVisible({ timeout: 15_000 });
  await expect(
    fileViewer.locator(".file-viewer-sync-state.state-synced"),
  ).toBeVisible();

  // The real proof: the file on disk (not just the DOM) reflects the save.
  expect(readFileSync(filePath, "utf-8")).toBe(updatedContent);
});
