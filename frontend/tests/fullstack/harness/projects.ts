import { expect, type Page } from "@playwright/test";

/**
 * Drives the real "+" add-project button (ProjectTabs.tsx's `onAdd` →
 * App.tsx's `setDirPickerOpen(true)`) through the real DirPickerModal:
 * types an absolute path into the address bar, browses to it (real
 * `GET /api/browse`), and picks it — landing on `handleAddProject`'s real
 * `POST /api/projects` (App.tsx:3699-3718, backend/projects_api.py).
 *
 * `absolutePath` must already exist on disk so the modal's "Select
 * directory" (not "Create directory") branch is exercised.
 */
export async function addProjectByPath(page: Page, absolutePath: string): Promise<void> {
  await page.locator(".project-tab-add").click();

  const modal = page.locator(".dir-picker-content");
  await modal.waitFor({ state: "visible", timeout: 10_000 });

  const addressInput = modal.locator(".dir-picker-address-bar input[type='text']");
  await addressInput.fill(absolutePath);
  await addressInput.press("Enter");

  const selectButton = modal.locator(".setup-save-btn");
  await expect(selectButton).toBeEnabled({ timeout: 10_000 });
  await selectButton.click();

  // handleAddProject only flips `dirPickerOpen` false in its `finally`,
  // after the real POST /api/projects + refetch resolve — waiting for the
  // modal to close means the project is actually persisted, not just
  // optimistically assumed.
  await modal.waitFor({ state: "hidden", timeout: 15_000 });
}
