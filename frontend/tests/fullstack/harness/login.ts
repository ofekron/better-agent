import type { Page } from "@playwright/test";
import type { FullStackBackend } from "./backend";

/** Drives the real Login.tsx form (not a token bypass) against a real backend. */
export async function loginViaUI(page: Page, backend: FullStackBackend): Promise<void> {
  await page.goto(backend.baseURL);
  await page.locator('input[autocomplete="username"]').fill(backend.username);
  await page.locator('input[autocomplete="current-password"]').fill(backend.password);
  await page.locator(".login-submit").click();
  await page.getByTestId("input-textarea").waitFor({ state: "visible", timeout: 15_000 });
}

/**
 * Real HTTP login (real argon2 password verification against
 * /api/auth/login, same as the form) plus dismissing the first-run wizard
 * before the SPA ever boots, so feature tests land straight on the Ask
 * session view instead of racing the app's own onboarding redirect to
 * /settings (see App.tsx's `first_run_wizard_done === false` effect). Use
 * `loginViaUI` instead when the test is specifically about the login screen.
 */
export async function loginAndSkipFirstRunWizard(
  page: Page,
  backend: FullStackBackend,
): Promise<void> {
  const loginRes = await page.request.post(`${backend.baseURL}/api/auth/login`, {
    data: { username: backend.username, password: backend.password },
  });
  if (!loginRes.ok()) {
    throw new Error(`login failed: ${loginRes.status()} ${await loginRes.text()}`);
  }
  const prefsRes = await page.request.patch(`${backend.baseURL}/api/user-prefs`, {
    data: { first_run_wizard_done: true },
  });
  if (!prefsRes.ok()) {
    throw new Error(`failed to seed first_run_wizard_done: ${prefsRes.status()}`);
  }
  await page.goto(backend.baseURL);
  // A fresh isolated home has zero sessions, so the composer legitimately
  // stays disabled ("Select or create a session to start...") — the real
  // readiness signal is the sidebar having finished loading, i.e. the "+
  // New" button (session creation) is interactive.
  await page.locator(".session-new-button").waitFor({ state: "visible", timeout: 30_000 });
}
