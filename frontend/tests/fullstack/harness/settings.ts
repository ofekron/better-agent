import type { Page } from "@playwright/test";

/** Opens the real edit view for a provider row in Settings > Providers. */
export async function openProviderSettings(page: Page, baseURL: string, kind: string): Promise<void> {
  await page.goto(`${baseURL}/settings`);
  await page.getByTestId(`provider-row-${kind}`).locator(".provider-row-main").click();
}

/** Saves the currently open provider edit form and waits for the real
 * PATCH /api/providers/{id} to actually complete (not just for the click
 * to register) before returning. */
export async function saveProviderSettings(page: Page): Promise<void> {
  const patched = page.waitForResponse(
    (res) => /\/api\/providers\/[^/]+$/.test(new URL(res.url()).pathname) && res.request().method() === "PATCH",
  );
  await page.locator(".modal-footer .setup-save-btn").click();
  await patched;
}

/** Picks an option (by exact visible label) from the app's custom
 * `Select` component (a button trigger + portal-rendered listbox, not a
 * native <select> — selectOption() does not apply). */
export async function pickCustomSelectOption(
  page: Page,
  triggerTestId: string,
  optionLabel: string,
): Promise<void> {
  await page.getByTestId(triggerTestId).click();
  await page.getByRole("option", { name: optionLabel, exact: true }).click();
}
