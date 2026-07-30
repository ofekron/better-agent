import { test, expect } from "./harness/fixtures";

// Validates enabling/disabling an already-installed bundled extension from
// Settings > Extensions against a REAL backend (real extension registry,
// real /api/extensions endpoints — no mocks). The catalog/marketplace flow
// (installing a NEW extension) is covered separately.
test.describe("extensions settings", () => {
  test("toggles a bundled extension off and back on, surviving a real page reload", async ({
    authedPage: page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();

    const rows = page.locator(".extension-ui-settings-row");
    await expect(rows.first()).toBeVisible();

    // Pick a row whose toggle isn't locked (`required` extensions can't be
    // disabled) so the test works against whatever's actually bundled.
    const toggleableRow = rows
      .filter({ has: page.locator('input[type="checkbox"]:not([disabled])') })
      .first();
    await expect(toggleableRow).toBeVisible();
    const extensionId = await toggleableRow.locator(".extension-ui-settings-id").innerText();
    const toggle = toggleableRow.locator(".extension-ui-settings-main-toggle input[type=\"checkbox\"]");

    // --- disable ---
    await expect(toggle).toBeChecked();
    await toggle.uncheck();
    await expect(toggleableRow).toHaveClass(/\bis-disabled\b/);
    await expect(toggle).not.toBeChecked();

    const rowById = () =>
      page
        .locator(".extension-ui-settings-row")
        .filter({ has: page.locator(".extension-ui-settings-id", { hasText: extensionId }) });

    // Confirm the backend actually persisted it, not just optimistic UI.
    const configAfterDisable = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/config`, {
        credentials: "include",
      });
      return res.json();
    }, extensionId);
    expect(configAfterDisable.enabled).toBe(false);

    // --- survives a real page reload ---
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();
    await expect(rowById()).toHaveClass(/\bis-disabled\b/);
    await expect(
      rowById().locator(".extension-ui-settings-main-toggle input[type=\"checkbox\"]"),
    ).not.toBeChecked();

    // --- re-enable ---
    await rowById()
      .locator(".extension-ui-settings-main-toggle input[type=\"checkbox\"]")
      .check();
    await expect(rowById()).not.toHaveClass(/\bis-disabled\b/);

    const configAfterEnable = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/config`, {
        credentials: "include",
      });
      return res.json();
    }, extensionId);
    expect(configAfterEnable.enabled).toBe(true);

    // --- re-enable also survives a reload ---
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();
    await expect(rowById()).not.toHaveClass(/\bis-disabled\b/);
    await expect(
      rowById().locator(".extension-ui-settings-main-toggle input[type=\"checkbox\"]"),
    ).toBeChecked();
  });
});
