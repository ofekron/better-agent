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

  // Uninstall is only offered for non-required extensions: SettingsPage.tsx
  // renders `.extension-ui-settings-uninstall` behind `!row.required`, so a
  // required row never has the button in the DOM at all (not merely
  // disabled). Backend-side, `REQUIRED_EXTENSION_IDS` currently contains only
  // the marketplace extension, so every other bundled extension is a real
  // non-required, marketplace-managed uninstall target.
  test("uninstalling a non-required extension removes it, and required extensions never expose an uninstall control", async ({
    authedPage: page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();

    const rows = page.locator(".extension-ui-settings-row");
    await expect(rows.first()).toBeVisible();

    // --- every required row has no uninstall button, not just a disabled one ---
    const requiredRows = rows.filter({ has: page.locator('input[type="checkbox"][disabled]') });
    const requiredCount = await requiredRows.count();
    expect(requiredCount).toBeGreaterThan(0);
    for (let i = 0; i < requiredCount; i++) {
      await expect(requiredRows.nth(i).locator(".extension-ui-settings-uninstall")).toHaveCount(0);
    }
    const requiredExtensionId = await requiredRows.first().locator(".extension-ui-settings-id").innerText();

    // --- REST defense in depth: bypassing the missing button still gets rejected ---
    const deleteRequiredResult = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}`, {
        method: "DELETE",
        credentials: "include",
      });
      return { status: res.status, body: await res.json() };
    }, requiredExtensionId);
    expect(deleteRequiredResult.status).toBe(400);
    expect(String(deleteRequiredResult.body.detail)).toMatch(/required/i);

    // --- pick a real non-required, marketplace-managed extension to uninstall ---
    const uninstallableRow = rows.filter({ has: page.locator(".extension-ui-settings-uninstall") }).first();
    await expect(uninstallableRow).toBeVisible();
    const extensionId = await uninstallableRow.locator(".extension-ui-settings-id").innerText();

    page.once("dialog", (dialog) => void dialog.accept());
    await uninstallableRow.locator(".extension-ui-settings-uninstall").click();

    const rowById = () =>
      page
        .locator(".extension-ui-settings-row")
        .filter({ has: page.locator(".extension-ui-settings-id", { hasText: extensionId }) });
    await expect(rowById()).toHaveCount(0);

    // --- confirm the backend actually removed it, not just optimistic UI ---
    // (extension_store.extension_config raises ExtensionError -> HTTP 400,
    // "Extension not installed" for an id that's no longer registered)
    const configAfterUninstall = await page.evaluate(async (id) => {
      const res = await fetch(`/api/extensions/${encodeURIComponent(id)}/config`, {
        credentials: "include",
      });
      return { status: res.status, body: await res.json() };
    }, extensionId);
    expect(configAfterUninstall.status).toBe(400);
    expect(String(configAfterUninstall.body.detail)).toMatch(/not installed/i);

    // --- survives a real page reload: it stays gone ---
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();
    await expect(rows.first()).toBeVisible();
    await expect(rowById()).toHaveCount(0);
  });

  // SettingsPage.tsx's ExtensionUiSettingsSection renders a real, uncontrolled
  // search box (`.extension-ui-settings-search-input`) backed by a `useMemo`
  // that filters `rows` by substring match against name/id/description on
  // every keystroke -- no debounce, no submit button. Validate that filtering
  // against a real, backend-fetched extension list.
  test("the extensions search box filters the visible rows by name/id in real time", async ({
    authedPage: page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/settings`);
    await page.getByTestId("settings-nav-extensions").click();

    const rows = page.locator(".extension-ui-settings-row");
    await expect(rows.first()).toBeVisible();

    const extractRows = () =>
      page.$$eval(".extension-ui-settings-row", (elements) =>
        elements.map((el) => ({
          id: el.querySelector(".extension-ui-settings-id")?.textContent?.trim() ?? "",
          name: el.querySelector(".extension-ui-settings-name")?.textContent?.trim() ?? "",
          description: el.querySelector(".extension-ui-settings-description")?.textContent?.trim() ?? "",
        })),
      );

    const allRows = await extractRows();
    expect(allRows.length).toBeGreaterThan(0);

    const searchInput = page.locator(".extension-ui-settings-search-input");
    await expect(searchInput).toBeVisible();

    // Pick a target row and derive a query from its id that is (almost
    // certainly) unique -- the last dot-separated segment of its id -- so we
    // can assert the filter is genuinely selective, not a no-op.
    const target = allRows[allRows.length - 1];
    const segments = target.id.split(/[.\-_]/).filter(Boolean);
    const query = (segments[segments.length - 1] || target.id).toLowerCase();

    const expectedIds = new Set(
      allRows
        .filter(({ id, name, description }) =>
          [name, id, description].some((v) => v.toLowerCase().includes(query)),
        )
        .map((r) => r.id),
    );
    expect(expectedIds.size).toBeGreaterThan(0);
    expect(expectedIds.has(target.id)).toBe(true);

    // --- typing filters in real time (no submit / blur needed) ---
    await searchInput.fill(query);
    await expect(rows).toHaveCount(expectedIds.size);
    const filteredRows = await extractRows();
    expect(new Set(filteredRows.map((r) => r.id))).toEqual(expectedIds);

    // --- a query matching nothing shows the empty-search hint and no rows ---
    await searchInput.fill("zzz-no-such-extension-zzz");
    await expect(rows).toHaveCount(0);
    await expect(page.locator(".extension-ui-settings-empty-search")).toBeVisible();

    // --- clearing the query restores the full, unfiltered list ---
    await searchInput.fill("");
    await expect(rows).toHaveCount(allRows.length);
    const restoredRows = await extractRows();
    expect(new Set(restoredRows.map((r) => r.id))).toEqual(new Set(allRows.map((r) => r.id)));
  });
});
