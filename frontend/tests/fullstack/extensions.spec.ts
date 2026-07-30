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

  test("a required extension's toggle is locked in the UI and rejected server-side", async ({
    authedPage: page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();

    const rows = page.locator(".extension-ui-settings-row");
    await expect(rows.first()).toBeVisible();

    // Required extensions (e.g. the built-in marketplace extension) render
    // their toggle as a disabled checkbox — find one instead of the
    // toggleable ones the other test exercises.
    const requiredRow = rows
      .filter({ has: page.locator('input[type="checkbox"][disabled]') })
      .first();
    await expect(requiredRow).toBeVisible();
    const extensionId = await requiredRow.locator(".extension-ui-settings-id").innerText();
    const toggle = requiredRow.locator(".extension-ui-settings-main-toggle input[type=\"checkbox\"]");

    // --- UI: the checkbox is genuinely disabled, not just styled that way ---
    await expect(toggle).toBeDisabled();
    await expect(toggle).toBeChecked();

    // --- REST: bypassing the UI entirely still gets rejected server-side ---
    const patchResult = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/enabled`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: false }),
      });
      return { status: res.status, body: await res.json() };
    }, extensionId);
    expect(patchResult.status).toBe(400);
    expect(String(patchResult.body.detail)).toMatch(/required/i);

    // --- no side effect: the extension is still reported enabled ---
    const catalogAfter = await page.evaluate(async () => {
      const res = await fetch(`/api/extensions`, { credentials: "include" });
      return res.json();
    });
    const extensionAfter = (catalogAfter.extensions ?? catalogAfter).find(
      (e: { id: string }) => e.id === extensionId,
    );
    expect(extensionAfter.enabled).toBe(true);
  });

  // The bundled `ofek-dev.user-attention` extension declares a real
  // manifest-driven config field (entrypoints.settings + settings_sections:
  // a `play_sound` boolean bound to the "notifications" app-settings
  // section). That's the generic extension-config UI: ExtensionAppSettingsSection
  // renders it, and every edit PATCHes /api/extensions/{id}/settings.
  test("a manifest-declared extension config value round-trips through Settings, and an invalid PATCH is rejected without corrupting storage", async ({
    authedPage: page,
    backend,
  }) => {
    const extensionId = "ofek-dev.user-attention";
    const settingKey = "play_sound";

    const fetchSettings = () =>
      page.evaluate(async (id) => {
        const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/settings`, {
          credentials: "include",
        });
        return res.json();
      }, extensionId);

    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-notifications").click();

    const checkbox = page.locator('.extension-app-settings-toggle input[type="checkbox"]');
    await expect(checkbox).toBeVisible();

    const initial = await fetchSettings();
    const initialValue: boolean = initial.values[settingKey];
    await expect(checkbox).toBeChecked({ checked: initialValue });

    // --- valid update round-trips through the UI to the real backend ---
    if (initialValue) {
      await checkbox.uncheck();
    } else {
      await checkbox.check();
    }
    await expect(checkbox).toBeChecked({ checked: !initialValue });
    expect((await fetchSettings()).values[settingKey]).toBe(!initialValue);

    // --- survives a real page reload ---
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-notifications").click();
    await expect(
      page.locator('.extension-app-settings-toggle input[type="checkbox"]'),
    ).toBeChecked({ checked: !initialValue });

    // --- REST: invalid config data (wrong type) is rejected server-side ---
    const invalidPatch = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/settings`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "play_sound", value: "not-a-boolean" }),
      });
      return { status: res.status, body: await res.json() };
    }, extensionId);
    expect(invalidPatch.status).toBe(400);
    expect(String(invalidPatch.body.detail)).toMatch(/boolean/i);

    // --- no side effect: the stored value is still what the valid update set ---
    expect((await fetchSettings()).values[settingKey]).toBe(!initialValue);

    // --- restore original value, confirming the round trip both ways ---
    await page.locator('.extension-app-settings-toggle input[type="checkbox"]').setChecked(initialValue);
    await expect(
      page.locator('.extension-app-settings-toggle input[type="checkbox"]'),
    ).toBeChecked({ checked: initialValue });
    expect((await fetchSettings()).values[settingKey]).toBe(initialValue);
  });
});
