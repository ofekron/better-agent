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

// Validates the draft round-trip specifically: an edit that is never
// explicitly saved still survives a real page reload, because the 1s-idle
// debounce in FileViewer.tsx POSTs it to `/api/file/draft`
// (backend/file_browser.py) — a real backend-persisted draft, not just React
// state. `fetchViewedTextFile` (FileViewer.tsx) fetches `GET /api/file/draft`
// alongside the disk read on every open and prefers it automatically when
// present — there is no "restore draft?" prompt to accept. The file on disk
// must stay untouched by the draft the whole time; only an explicit save
// writes it.
test("persists an unsaved draft across a real page reload, before the explicit save", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace-draft");
  mkdirSync(workspaceDir, { recursive: true });
  const filePath = path.join(workspaceDir, "draft-notes.txt");
  const originalContent = "original content on disk";
  writeFileSync(filePath, originalContent);

  await addProjectByPath(page, workspaceDir);
  await createSessionWithPrompt(page, "Reply with exactly the single word: OK.");

  const openFile = async () => {
    await page.getByTitle("Browse project files").click();
    const chooser = page.locator(".file-chooser-content");
    await expect(chooser).toBeVisible();
    const fileRow = chooser.locator(".tree-node.tree-file", { hasText: "draft-notes.txt" });
    await fileRow.click();
    await expect(chooser).not.toBeVisible();
  };

  await openFile();

  const fileViewer = page.locator(".file-viewer");
  await expect(fileViewer).toBeVisible();
  const editor = fileViewer.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });
  await expect(editor.locator(".view-lines")).toContainText(originalContent, { timeout: 15_000 });

  const draftContent = "unsaved draft content, never explicitly saved";
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(draftContent);

  // Wait for the real debounced autosave (fires after >1s idle, per
  // FileViewer.tsx's `saveDebounceRef` timeout) to actually POST to
  // `/api/file/draft` and resolve — an event to react to, not a sleep.
  await page.waitForResponse(
    (res) =>
      res.url().includes("/api/file/draft") &&
      res.request().method() === "POST" &&
      res.ok(),
    { timeout: 10_000 },
  );

  // Draft != save: the real file on disk must still be untouched.
  expect(readFileSync(filePath, "utf-8")).toBe(originalContent);

  await page.reload();
  await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

  await openFile();
  const reopenedViewer = page.locator(".file-viewer");
  await expect(reopenedViewer).toBeVisible();
  const reopenedEditor = reopenedViewer.locator(".monaco-editor").first();
  await expect(reopenedEditor).toBeVisible({ timeout: 15_000 });

  // The editor shows the DRAFT content, not the on-disk original — proving
  // the draft was fetched back from the backend after the reload, not
  // recovered from in-memory React state (which the reload destroyed).
  await expect(reopenedEditor.locator(".view-lines")).toContainText(draftContent, {
    timeout: 15_000,
  });

  // Still untouched on disk — the draft lives only in the backend's draft
  // store until an explicit save.
  expect(readFileSync(filePath, "utf-8")).toBe(originalContent);

  // Close the loop: an explicit save now writes the draft content to disk.
  const saveButton = reopenedViewer.locator('button[title="Save (Cmd+S)"]');
  await expect(saveButton).toBeEnabled();
  await saveButton.click();

  await expect(reopenedViewer.locator(".file-viewer-dirty")).not.toBeVisible({ timeout: 15_000 });
  await expect(
    reopenedViewer.locator(".file-viewer-sync-state.state-synced"),
  ).toBeVisible();

  expect(readFileSync(filePath, "utf-8")).toBe(draftContent);
});

// Validates the app's actual (lack of) conflict handling: FileViewer.tsx
// tracks `loadedIdentity` vs. `currentIdentity` (mtime_ns/size from
// `GET /api/file/metadata`, polled every 5s — see the `poll()` effect) and
// flags a `stale` UI badge (".file-viewer-stale", "Changed since loaded")
// plus an opt-in "Update to latest" button when they diverge. But `save()`
// itself sends no identity/mtime with its `POST /api/file` body, and
// `backend/file_browser.py::write_file_content` performs an unconditional
// `path.write_text(...)` with no compare-and-swap against the metadata the
// editor loaded — confirmed by reading both. So: an external change while
// the editor has unsaved local edits gets a passive "stale" hint, but
// clicking Save is never blocked and silently overwrites the externally
// written content with the local buffer. This test locks in that real,
// current behavior (not a desired one).
test("silently overwrites an externally-modified file on save, with only a passive staleness hint", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace-conflict");
  mkdirSync(workspaceDir, { recursive: true });
  const filePath = path.join(workspaceDir, "conflict-notes.txt");
  const originalContent = "original content on disk";
  writeFileSync(filePath, originalContent);

  await addProjectByPath(page, workspaceDir);
  await createSessionWithPrompt(page, "Reply with exactly the single word: OK.");

  await page.getByTitle("Browse project files").click();
  const chooser = page.locator(".file-chooser-content");
  await expect(chooser).toBeVisible();
  const fileRow = chooser.locator(".tree-node.tree-file", { hasText: "conflict-notes.txt" });
  await fileRow.click();
  await expect(chooser).not.toBeVisible();

  const fileViewer = page.locator(".file-viewer");
  await expect(fileViewer).toBeVisible();
  const editor = fileViewer.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });
  await expect(editor.locator(".view-lines")).toContainText(originalContent, { timeout: 15_000 });

  // Make a local, unsaved edit in the browser.
  const localEditContent = "local unsaved edit, made before the external change";
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(localEditContent);
  await expect(fileViewer.locator(".file-viewer-dirty")).toBeVisible();

  // Directly modify the REAL file on disk from Node, out from under the
  // editor — e.g. simulating a concurrent `git pull` or another process.
  const externalContent = "externally written content, e.g. from a git pull";
  writeFileSync(filePath, externalContent);

  // The editor's 5s metadata poll (FileViewer.tsx's `poll()` effect) picks
  // up the mtime/size divergence and flips the passive "stale" badge —
  // an event to wait for, not a sleep.
  await expect(fileViewer.locator(".file-viewer-stale")).toBeVisible({ timeout: 10_000 });
  // The local edit is still in the buffer; nothing has been overwritten yet.
  await expect(editor.locator(".view-lines")).toContainText(localEditContent);

  // Now Save. There is no conflict prompt, no merge UI, no blocked save —
  // the Save button stays enabled and clicking it proceeds unconditionally.
  const saveButton = fileViewer.locator('button[title="Save (Cmd+S)"]');
  await expect(saveButton).toBeEnabled();
  await saveButton.click();

  await expect(fileViewer.locator(".file-viewer-dirty")).not.toBeVisible({ timeout: 15_000 });
  await expect(
    fileViewer.locator(".file-viewer-sync-state.state-synced"),
  ).toBeVisible();

  // The real proof: the externally-written content is gone. The local
  // buffer clobbered it with no conflict detection at the point of save.
  expect(readFileSync(filePath, "utf-8")).toBe(localEditContent);
});

// Validates the app's actual (lack of) binary handling: FileViewer.tsx's
// `categorize()` only special-cases markdown/csv/tsv/pdf/video extensions
// and JSON content — there is no "image" ViewerKind and no content-type or
// decode-failure check anywhere in the file. A file with an image extension
// (or any other non-special extension) always falls through to `kind ===
// "code"` and is fetched as TEXT via `GET /api/file` → `get_file_content`
// (backend/file_browser.py), which reads with
// `path.read_text(encoding="utf-8", errors="replace")` — invalid byte
// sequences become U+FFFD replacement characters, never an exception. So
// the real "fallback" for a binary file is: it opens in the same Monaco
// text editor as any other file, showing the mangled decode instead of a
// dedicated binary/unsupported-file message. This test locks in that real,
// current behavior and proves it doesn't crash the app.
test("opens a real binary file without crashing, falling back to a mangled text decode", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace-binary");
  mkdirSync(workspaceDir, { recursive: true });
  const filePath = path.join(workspaceDir, "image.png");

  // Real binary bytes: the genuine 8-byte PNG signature followed by every
  // byte value 0-255 twice over. Bytes >= 0x80 that don't form a valid
  // UTF-8 sequence (most of this range, taken byte-by-byte) are exactly
  // what forces Python's `errors="replace"` decode to emit U+FFFD.
  const pngSignature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  const allByteValues = Array.from({ length: 256 }, (_, i) => i);
  const binaryBytes = Buffer.from([...pngSignature, ...allByteValues, ...allByteValues]);
  writeFileSync(filePath, binaryBytes);

  await addProjectByPath(page, workspaceDir);
  await createSessionWithPrompt(page, "Reply with exactly the single word: OK.");

  await page.getByTitle("Browse project files").click();
  const chooser = page.locator(".file-chooser-content");
  await expect(chooser).toBeVisible();
  const fileRow = chooser.locator(".tree-node.tree-file", { hasText: "image.png" });
  await fileRow.click();
  await expect(chooser).not.toBeVisible();

  // No crash: the file viewer mounts and renders the file in Monaco (the
  // "code" kind is the fallback for any extension categorize() doesn't
  // recognize) instead of a blank page or an uncaught exception.
  const fileViewer = page.locator(".file-viewer");
  await expect(fileViewer).toBeVisible();
  await expect(fileViewer.locator(".file-viewer-path")).toContainText("image.png");
  const editor = fileViewer.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });

  // The graceful fallback: the invalid UTF-8 bytes were decoded with
  // replacement characters, not rejected — real evidence the backend read
  // the binary content successfully and the frontend rendered it as-is.
  await expect(editor.locator(".view-lines")).toContainText("�", { timeout: 15_000 });

  // The rest of the app is still alive and interactive after loading
  // binary content — no uncaught exception broke React. Re-open the file
  // chooser (sidebar control)...
  await page.getByTitle("Browse project files").click();
  await expect(chooser).toBeVisible();
  await page.locator(".modal-close").click();
  await expect(chooser).not.toBeVisible();

  // ...and the chat composer still accepts input.
  const composer = page.getByTestId("input-textarea");
  await composer.fill("still alive after opening a binary file");
  await expect(composer).toHaveValue("still alive after opening a binary file");
});

// Validates the real "create a brand-new file" path (not editing an
// existing one): the file chooser itself has a create affordance —
// FileTree.tsx renders a `.file-tree-create-row` (kind <select> + name
// <input> + Create button, `allowCreate` defaults to true) whenever the
// chooser isn't mid-search. Submitting it POSTs `/api/files/create`
// (backend/file_api.py `create_file_entry` → `file_browser.create_file`,
// which does a real `Path.touch()`), then `createEntry()` in FileTree.tsx
// calls `onFileClick(created.path)` to open the brand-new file straight
// into the same Monaco editor used for existing files. This test proves
// the file did not exist on disk beforehand, creates it purely through
// the UI, and confirms both the DOM (editor opens on an empty new file)
// and the real filesystem reflect the creation.
test("creates a brand-new file through the file chooser's create row and opens it", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace-create");
  mkdirSync(workspaceDir, { recursive: true });
  const newFileName = "brand-new-file.txt";
  const newFilePath = path.join(workspaceDir, newFileName);

  await addProjectByPath(page, workspaceDir);
  await createSessionWithPrompt(page, "Reply with exactly the single word: OK.");

  await page.getByTitle("Browse project files").click();
  const chooser = page.locator(".file-chooser-content");
  await expect(chooser).toBeVisible();

  // Real proof the file doesn't exist yet, before we create it.
  await expect(
    chooser.locator(".tree-node.tree-file", { hasText: newFileName }),
  ).toHaveCount(0);

  const createRow = chooser.locator(".file-tree-create-row");
  await expect(createRow).toBeVisible();
  // Default kind is already "file" (FileTree.tsx's `createKind` state), so
  // no need to touch the kind <select>.
  const nameInput = createRow.locator("input[type='text']");
  await nameInput.fill(newFileName);
  const createButton = createRow.locator("button");
  await expect(createButton).toBeEnabled();
  await createButton.click();

  // createEntry() closes the chooser and opens the new file automatically.
  await expect(chooser).not.toBeVisible();

  const fileViewer = page.locator(".file-viewer");
  await expect(fileViewer).toBeVisible();
  await expect(fileViewer.locator(".file-viewer-path")).toContainText(newFileName);

  const editor = fileViewer.locator(".monaco-editor").first();
  await expect(editor).toBeVisible({ timeout: 15_000 });

  // The real proof: the backend's `Path.touch()` actually created the file
  // on disk, empty, before any content was ever typed into the editor.
  await expect.poll(() => readFileSync(newFilePath, "utf-8")).toBe("");

  // Prove the editor is genuinely wired to this new file: typing and
  // saving writes real content to the file `create_file` produced.
  const content = "content typed into a freshly created file";
  await editor.click();
  await page.keyboard.type(content);
  await expect(fileViewer.locator(".file-viewer-dirty")).toBeVisible();

  const saveButton = fileViewer.locator('button[title="Save (Cmd+S)"]');
  await expect(saveButton).toBeEnabled();
  await saveButton.click();

  await expect(fileViewer.locator(".file-viewer-dirty")).not.toBeVisible({ timeout: 15_000 });
  expect(readFileSync(newFilePath, "utf-8")).toBe(content);
});
