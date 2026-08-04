import type { Page } from "@playwright/test";

export interface CreateSessionOptions {
  /** Case-insensitive substring/regex matched against the model dropdown's
   * option labels. Defaults to a fast/cheap model so the many full-stack
   * specs that just need *a* real turn don't all pay for a slow one. */
  modelPattern?: RegExp;
  /** Pattern matched against the runtime-profile dropdown's option labels.
   * When omitted the backend's default runtime profile (pre-selected by the
   * modal) is used. */
  profilePattern?: RegExp;
}

/** The session record POST /api/sessions returns (subset the specs assert). */
export interface CreatedSessionRecord {
  id: string;
  runtime_profile_id?: string | null;
  model?: string;
}

const DEFAULT_FAST_MODEL = /haiku/i;
const CREATE_ENABLED_TIMEOUT_MS = 20_000;
const OPTION_AVAILABLE_TIMEOUT_MS = 15_000;
const MAX_OPEN_ATTEMPTS = 3;

async function selectOptionMatching(
  page: Page,
  testId: string,
  pattern: RegExp,
  required: boolean,
): Promise<void> {
  if (required) {
    await page.waitForFunction(
      ({ selector, source, flags }) => {
        const select = document.querySelector<HTMLSelectElement>(
          `[data-testid="${selector}"]`,
        );
        return Array.from(select?.options ?? []).some((option) =>
          new RegExp(source, flags).test(option.textContent ?? ""),
        );
      },
      { selector: testId, source: pattern.source, flags: pattern.flags },
      { timeout: OPTION_AVAILABLE_TIMEOUT_MS },
    );
  }
  const select = page.getByTestId(testId);
  const options = select.locator("option");
  const count = await options.count();
  for (let i = 0; i < count; i++) {
    const option = options.nth(i);
    const text = (await option.textContent()) ?? "";
    if (new RegExp(pattern.source, pattern.flags).test(text)) {
      const value = await option.getAttribute("value");
      if (value !== null) {
        await select.selectOption(value);
        return;
      }
    }
  }
  if (required) throw new Error(`required option ${pattern} missing from ${testId}`);
}

/**
 * Configures the modal's runtime-profile picker (the single session-identity
 * selector — the old provider/runner pickers no longer exist) and the model
 * override under it. The default profile id resolves asynchronously from the
 * /api/runtime-profiles snapshot, so wait for a real selection before
 * touching the dependent model select (its options derive from the selected
 * profile's provider catalog).
 */
async function configureSessionSelectors(page: Page, options: CreateSessionOptions): Promise<void> {
  await page.getByTestId("new-session-profile-select").waitFor({ state: "visible", timeout: 10_000 });
  await page.waitForFunction(
    () => {
      const el = document.querySelector<HTMLSelectElement>(
        '[data-testid="new-session-profile-select"]',
      );
      return !!el && el.value !== "";
    },
    { timeout: 15_000 },
  );
  if (options.profilePattern) {
    await selectOptionMatching(page, "new-session-profile-select", options.profilePattern, true);
  }
  await selectOptionMatching(
    page,
    "new-session-model-select",
    options.modelPattern ?? DEFAULT_FAST_MODEL,
    options.modelPattern !== undefined,
  );
}

/**
 * Drives "+ New" → NewSessionModal open, waits for the runtime-profile
 * defaults to resolve, applies the profile/model selection, and waits for
 * the primary create action to become enabled.
 *
 * NewSessionModal.tsx fetches GET /api/providers and the runtime-profiles
 * snapshot when it opens; the fetches retry transient failures with backoff
 * and a persistent failure surfaces a visible error with its own in-app
 * retry button, but this harness intentionally does not depend on that UI
 * path. Rather than let any stall on `.ns-create-primary` wedge every test
 * that creates a session, this closes and reopens the modal (which
 * re-triggers the fetches) a bounded number of times if the button hasn't
 * enabled within a generous window — an independent safety net for test
 * reliability.
 */
async function openConfiguredNewSessionModal(page: Page, options: CreateSessionOptions): Promise<void> {
  const primaryButton = page.locator(".ns-create-primary");
  const promptBox = page.getByTestId("new-session-prompt-textarea");

  for (let attempt = 1; attempt <= MAX_OPEN_ATTEMPTS; attempt++) {
    await page.locator(".session-new-button").click();
    await promptBox.waitFor({ state: "visible", timeout: 10_000 });

    try {
      await configureSessionSelectors(page, options);
      await primaryButton.waitFor({ state: "visible", timeout: 5_000 });
      await page.waitForFunction(
        () => {
          const el = document.querySelector<HTMLButtonElement>(".ns-create-primary");
          return !!el && !el.disabled;
        },
        { timeout: CREATE_ENABLED_TIMEOUT_MS },
      );
      return;
    } catch (err) {
      if (attempt === MAX_OPEN_ATTEMPTS) throw err;
      await page.locator(".modal-close").click();
      await promptBox.waitFor({ state: "hidden", timeout: 10_000 });
    }
  }
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
): Promise<CreatedSessionRecord> {
  await openConfiguredNewSessionModal(page, options);
  await page.getByTestId("new-session-prompt-textarea").fill(prompt);
  const created = page.waitForResponse(
    (res) => new URL(res.url()).pathname === "/api/sessions" && res.request().method() === "POST",
  );
  await page.locator(".ns-create-primary").click();
  const res = await created;
  if (!res.ok()) {
    throw new Error(`POST /api/sessions failed: ${res.status()} ${await res.text()}`);
  }
  const session = (await res.json()) as CreatedSessionRecord;
  await page.waitForURL((url) => url.pathname === `/s/${session.id}`, { timeout: 20_000 });
  await page
    .getByTestId("new-session-prompt-textarea")
    .waitFor({ state: "hidden", timeout: 20_000 });
  await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });
  return session;
}

/**
 * Creates a real session through the modal's split-button "Create" action —
 * no initial prompt is sent, so NO provider CLI turn is spent. Use this for
 * structural assertions (e.g. which runtime profile a session was created
 * from) that must stay runnable without LLM-gated approval.
 */
export async function createSessionWithoutTurn(
  page: Page,
  options: CreateSessionOptions = {},
): Promise<CreatedSessionRecord> {
  await openConfiguredNewSessionModal(page, options);

  const created = page.waitForResponse(
    (res) => new URL(res.url()).pathname === "/api/sessions" && res.request().method() === "POST",
  );
  await page.locator(".ns-create-toggle").click();
  await page.getByRole("menuitem", { name: "Create", exact: true }).click();
  const res = await created;
  if (!res.ok()) {
    throw new Error(`POST /api/sessions failed: ${res.status()} ${await res.text()}`);
  }
  const session = (await res.json()) as CreatedSessionRecord;

  // The modal closes itself once the create lands.
  await page.getByTestId("new-session-prompt-textarea").waitFor({ state: "hidden", timeout: 10_000 });
  return session;
}
