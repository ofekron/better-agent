import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
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
});
