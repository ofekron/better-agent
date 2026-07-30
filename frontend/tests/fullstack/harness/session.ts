import type { Page } from "@playwright/test";

export interface CreateSessionOptions {
  /** Case-insensitive substring/regex matched against the model dropdown's
   * option labels. Defaults to a fast/cheap model so the many full-stack
   * specs that just need *a* real turn don't all pay for a slow one. */
  modelPattern?: RegExp;
}

const DEFAULT_FAST_MODEL = /haiku/i;
const CREATE_ENABLED_TIMEOUT_MS = 20_000;
const MAX_OPEN_ATTEMPTS = 3;

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
 *
 * NewSessionModal.tsx fetches GET /api/providers when it opens; the fetch
 * itself retries transient failures with backoff and a persistent failure
 * surfaces a visible error with its own in-app retry button, but this
 * harness intentionally does not depend on that UI path. Rather than let
 * any stall on `.ns-create-primary` wedge every test that creates a
 * session, this closes and reopens the modal (which re-triggers the
 * fetch) a bounded number of times if the button hasn't enabled within a
 * generous window — an independent safety net for test reliability.
 */
export async function createSessionWithPrompt(
  page: Page,
  prompt: string,
  options: CreateSessionOptions = {},
): Promise<void> {
  const primaryButton = page.locator(".ns-create-primary");
  const promptBox = page.getByTestId("new-session-prompt-textarea");

  for (let attempt = 1; attempt <= MAX_OPEN_ATTEMPTS; attempt++) {
    await page.locator(".session-new-button").click();
    await promptBox.waitFor({ state: "visible", timeout: 10_000 });
    await selectOptionMatching(page, "new-session-model-select", options.modelPattern ?? DEFAULT_FAST_MODEL);
    await promptBox.fill(prompt);

    try {
      await primaryButton.waitFor({ state: "visible", timeout: 5_000 });
      await page.waitForFunction(
        () => {
          const el = document.querySelector<HTMLButtonElement>(".ns-create-primary");
          return !!el && !el.disabled;
        },
        { timeout: CREATE_ENABLED_TIMEOUT_MS },
      );
      break;
    } catch (err) {
      if (attempt === MAX_OPEN_ATTEMPTS) throw err;
      await page.locator(".modal-close").click();
      await promptBox.waitFor({ state: "hidden", timeout: 10_000 });
    }
  }

  await primaryButton.click();
  await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });
}
