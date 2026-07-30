import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import { createRealGitRepo } from "./harness/git-repo";
import { addProjectByPath } from "./harness/projects";

// Validates the real "project" feature — repo-path tabs in the sidebar
// (frontend/src/components/ProjectTabs.tsx, backend/projects_api.py). A
// project is added by driving the real "+" → DirPickerModal flow against a
// real directory on disk, then verified both in the live DOM and via a real
// backend refetch and a full page reload (not client routing), proving the
// tab is server-persisted state rather than optimistic client-only state.
test.describe("projects", () => {
  let projectDir: string;

  test.beforeEach(() => {
    projectDir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-project-"));
  });

  test.afterEach(() => {
    rmSync(projectDir, { recursive: true, force: true });
  });

  test("adding a project shows it as a sidebar tab and survives a real reload", async ({
    authedPage: page,
    backend,
  }) => {
    const label = path.basename(projectDir);

    await addProjectByPath(page, projectDir);

    const tab = page.locator(".project-tab", { hasText: label });
    await expect(tab).toBeVisible();
    await expect(tab.locator(".project-tab-label")).toHaveText(label);

    // Confirm the backend actually persisted it (project_store), not just
    // optimistic client state.
    const projectsRes = await page.request.get(`${backend.baseURL}/api/projects`);
    expect(projectsRes.ok()).toBeTruthy();
    const projectsBody = await projectsRes.json();
    const persisted = (projectsBody.projects as Array<{ path: string }>).some((p) =>
      p.path.endsWith(label),
    );
    expect(persisted).toBeTruthy();

    // Real reload — a fresh GET /api/projects on mount, not leftover
    // client-side state — must reproduce the tab.
    await page.goto(backend.baseURL);
    await page.locator(".session-new-button").waitFor({ state: "visible", timeout: 30_000 });

    const tabAfterReload = page.locator(".project-tab", { hasText: label });
    await expect(tabAfterReload).toBeVisible();
    await expect(tabAfterReload.locator(".project-tab-label")).toHaveText(label);
  });

  test("removing a project via the manage modal deletes it server-side", async ({
    authedPage: page,
    backend,
  }) => {
    const label = path.basename(projectDir);

    await addProjectByPath(page, projectDir);

    const tab = page.locator(".project-tab", { hasText: label });
    await expect(tab).toBeVisible();

    // Drive the real "manage" modal (ProjectTabs.tsx's `.project-tab-manage`
    // trigger → `.project-list-modal`): select the project's row, then hit
    // the real DELETE /api/projects via the danger button.
    await page.locator(".project-tab-manage").click();
    const modal = page.locator(".project-list-modal");
    await modal.waitFor({ state: "visible", timeout: 10_000 });

    const row = modal.locator(".project-list-modal-row", { hasText: label });
    await row.locator("input[type='checkbox']").check();

    const deleteButton = modal.locator(".project-list-modal-danger");
    await expect(deleteButton).toBeEnabled();
    await deleteButton.click();

    // deleteSelectedProjects awaits the real DELETE /api/projects for each
    // selected project before closing the modal, so a hidden modal means
    // the backend call has already resolved.
    await modal.waitFor({ state: "hidden", timeout: 15_000 });

    await expect(tab).toBeHidden();

    // Confirm the backend actually removed it (project_store), not just
    // optimistic client state.
    const projectsRes = await page.request.get(`${backend.baseURL}/api/projects`);
    expect(projectsRes.ok()).toBeTruthy();
    const projectsBody = await projectsRes.json();
    const stillPersisted = (projectsBody.projects as Array<{ path: string }>).some((p) =>
      p.path.endsWith(label),
    );
    expect(stillPersisted).toBeFalsy();

    // Real reload — the removal must not be undone by a fresh
    // GET /api/projects on mount.
    await page.goto(backend.baseURL);
    await page.locator(".session-new-button").waitFor({ state: "visible", timeout: 30_000 });

    const tabAfterReload = page.locator(".project-tab", { hasText: label });
    await expect(tabAfterReload).toBeHidden();
  });

  test("switching between two projects scopes the file browser to whichever is selected", async ({
    authedPage: page,
  }) => {
    // Two separate real directories, each with a distinctly-named file, so
    // a stale/mixed file browser would be caught by seeing the wrong file
    // (or both files) rather than just the right one.
    const dirA = mkdtempSync(path.join(tmpdir(), "ba-fullstack-project-a-"));
    const dirB = mkdtempSync(path.join(tmpdir(), "ba-fullstack-project-b-"));
    try {
      writeFileSync(path.join(dirA, "alpha.txt"), "alpha");
      writeFileSync(path.join(dirB, "beta.txt"), "beta");

      const labelA = path.basename(dirA);
      const labelB = path.basename(dirB);

      await addProjectByPath(page, dirA);
      await addProjectByPath(page, dirB);

      const tabA = page.locator(".project-tab", { hasText: labelA });
      const tabB = page.locator(".project-tab", { hasText: labelB });

      // handleAddProject (App.tsx) selects the just-added project, so
      // adding B second leaves B active — this is itself part of what we
      // assert below rather than assumed.
      await expect(tabB).toHaveClass(/\bactive\b/);
      await expect(tabA).not.toHaveClass(/\bactive\b/);

      const assertFileChooserShowsOnly = async (visibleFile: string, hiddenFile: string) => {
        // App.tsx's sidebar folder button opens FileChooserModal scoped to
        // its `cwd` state, which handleSelectProject (ProjectTabs' onSelect)
        // sets synchronously to the clicked project's path — this is the
        // real project-scoped file browser, not a mock.
        await page.getByTitle("Browse project files").click();
        const chooser = page.locator(".file-chooser-content");
        await expect(chooser).toBeVisible();

        await expect(
          chooser.locator(".tree-node.tree-file", { hasText: visibleFile }),
        ).toBeVisible();
        await expect(
          chooser.locator(".tree-node.tree-file", { hasText: hiddenFile }),
        ).toHaveCount(0);

        await chooser.locator(".modal-close").click();
        await expect(chooser).toBeHidden();
      };

      // B is already the active tab — verify its file browser shows only
      // beta.txt, not alpha.txt from the other project.
      await assertFileChooserShowsOnly("beta.txt", "alpha.txt");

      // Switch to A via its tab (ProjectTabs' onSelect → handleSelectProject
      // → real POST /api/projects/touch) and confirm the file browser now
      // scopes to A only — not a stale mix of both.
      await tabA.click();
      await expect(tabA).toHaveClass(/\bactive\b/);
      await expect(tabB).not.toHaveClass(/\bactive\b/);
      await assertFileChooserShowsOnly("alpha.txt", "beta.txt");

      // Switch back to B and confirm the scope flips back cleanly — proving
      // this isn't a one-directional artifact of initial selection.
      await tabB.click();
      await expect(tabB).toHaveClass(/\bactive\b/);
      await expect(tabA).not.toHaveClass(/\bactive\b/);
      await assertFileChooserShowsOnly("beta.txt", "alpha.txt");
    } finally {
      rmSync(dirA, { recursive: true, force: true });
      rmSync(dirB, { recursive: true, force: true });
    }
  });

  test("adding the same directory twice dedupes instead of crashing or duplicating the tab", async ({
    authedPage: page,
    backend,
  }) => {
    // project_store.add_project (backend/project_store.py) upserts by
    // (node_id, normalized path): a second POST /api/projects for the same
    // path hits the existing-record branch, refreshes `last_used`, and
    // returns the same record rather than inserting a second one.
    const label = path.basename(projectDir);

    await addProjectByPath(page, projectDir);
    await addProjectByPath(page, projectDir);

    const tabs = page.locator(".project-tab", { hasText: label });
    await expect(tabs).toHaveCount(1);
    await expect(tabs).toBeVisible();

    const projectsRes = await page.request.get(`${backend.baseURL}/api/projects`);
    expect(projectsRes.ok()).toBeTruthy();
    const projectsBody = await projectsRes.json();
    const matches = (projectsBody.projects as Array<{ path: string }>).filter((p) =>
      p.path.endsWith(label),
    );
    expect(matches).toHaveLength(1);
  });

  test("zero projects renders a clean empty state with just the add button", async ({
    authedPage: page,
    backend,
  }) => {
    // authedPage's backend is freshly spawned per test, so no project has
    // been added yet — this is the real first-run empty state, not a mock.
    await expect(page.locator(".project-tab")).toHaveCount(0);
    await expect(page.locator(".project-tab-add")).toBeVisible();

    const projectsRes = await page.request.get(`${backend.baseURL}/api/projects`);
    expect(projectsRes.ok()).toBeTruthy();
    const projectsBody = await projectsRes.json();
    expect(projectsBody.projects).toEqual([]);
  });

  test("registering a real git repo with a remote captures git_remote from the checkout", async ({
    authedPage: page,
    backend,
  }) => {
    // project_store.add_project (backend/project_store.py) calls
    // ensure_git_remote -> probe_git_remote on the real checkout, which
    // shells out to `git -C <path> remote` / `remote get-url <name>`
    // (preferring `origin`) and stores the raw URL string verbatim as
    // `git_remote` on the persisted record.
    const repo = createRealGitRepo();
    const remoteUrl = "https://example.com/fake/repo.git";
    try {
      execFileSync("git", ["remote", "add", "origin", remoteUrl], { cwd: repo.dir });

      const label = path.basename(repo.dir);
      await addProjectByPath(page, repo.dir);

      const tab = page.locator(".project-tab", { hasText: label });
      await expect(tab).toBeVisible();

      const projectsRes = await page.request.get(`${backend.baseURL}/api/projects`);
      expect(projectsRes.ok()).toBeTruthy();
      const projectsBody = await projectsRes.json();
      const project = (
        projectsBody.projects as Array<{ path: string; git_remote: string | null }>
      ).find((p) => p.path.endsWith(label));
      expect(project).toBeDefined();
      expect(project?.git_remote).toBe(remoteUrl);
    } finally {
      repo.cleanup();
    }
  });
});
