import type { Page } from "@playwright/test";

export interface CreateSessionOptions {
  /** Case-insensitive substring/regex matched against the model dropdown's
   * option labels. Defaults to a fast/cheap model so the many full-stack
   * specs that just need *a* real turn don't all pay for a slow one. */
  modelPattern?: RegExp;
}

const DEFAULT_FAST_MODEL = /haiku/i;

async function selectOptionMatching(page: Page, testId: string, pattern: RegExp): Promise<void> {
  const select = page.getByTestId(testId);
  const options = select.locator("option");
  const count = await options.count();
  for (let i = 0; i < count; i++) {
    const option = options.nth(i);
    const text = (await option.textContent()) ?? "";
    if (pattern.test(text)) {
      const value = await option.getAttribute("value");
      if (value !== null) {
        await select.selectOption(value);
        return;
      }
    }
  }
  // No match (e.g. the pattern doesn't exist in this catalog) — leave the
  // provider's own default selected rather than failing the whole flow.
}

/**
 * Drives the real "+ New" → NewSessionModal → "Create & Send & Open" flow
 * (the default primary action) to create a real session and send its first
 * prompt through a real provider CLI turn, landing on that session's chat
 * view.
 */
export async function createSessionWithPrompt(
  page: Page,
  prompt: string,
  options: CreateSessionOptions = {},
): Promise<void> {
  await page.locator(".session-new-button").click();
  const promptBox = page.getByTestId("new-session-prompt-textarea");
  await promptBox.waitFor({ state: "visible", timeout: 10_000 });
  await selectOptionMatching(page, "new-session-model-select", options.modelPattern ?? DEFAULT_FAST_MODEL);
  await promptBox.fill(prompt);
  await page.locator(".ns-create-primary").click();
  await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });
}
