import { test, expect } from "./harness/fixtures";
import { loginViaUI } from "./harness/login";
import { randomTestValue } from "./harness/credentials";

// Drives the real Settings > Account > "Change credentials" form
// (AuthCredentialsSetting.tsx) against a real backend, then proves the
// change actually took effect against the real argon2id-hashed credential
// store: the old username/password stop working and the new ones work,
// both verified through the real Login.tsx form (not a token bypass).
test.describe("change credentials", () => {
  test("rotates username and password and enforces them on next login", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    const newUsername = randomTestValue("changed-user");
    const newPassword = randomTestValue("changed-secret");

    // Both the "current username" and "new username" fields use
    // autocomplete="username" (matching Login.tsx's convention), so the
    // two are disambiguated by DOM order (.nth) within the scoped form;
    // the password fields have distinct autocomplete values already.
    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(backend.password);
    await form.locator('input[autocomplete="username"]').nth(1).fill(newUsername);
    await form.locator('input[autocomplete="new-password"]').fill(newPassword);
    await form.locator(".setup-save-btn").click();

    await expect(form.locator(".auth-credentials-status")).toBeVisible();
    await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

    // Log out fully. The real /api/auth/logout endpoint only clears the
    // session cookie (auth_routes.logout: `request.session.clear()`) but
    // the save above just stored a fresh bearer token in localStorage
    // (AuthCredentialsSetting -> setStoredToken), which the bearer
    // interceptor (bearerAuth.ts) attaches as `Authorization: Bearer`
    // to every request regardless of the cookie. Clearing only the
    // cookie would let the stale bearer token silently keep the session
    // "authenticated" and invalidate the assertions below.
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });

    // The old credentials must now be rejected.
    await page.goto(backend.baseURL);
    await page.locator('input[autocomplete="username"]').fill(backend.username);
    await page.locator('input[autocomplete="current-password"]').fill(backend.password);
    await page.locator(".login-submit").click();
    await expect(page.locator(".login-error")).toBeVisible();
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();

    // The new credentials must work, through the real login form.
    await page.goto(backend.baseURL);
    await loginViaUI(page, { ...backend, username: newUsername, password: newPassword });

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(newUsername);
  });

  test("rejects a wrong current password and leaves credentials unchanged", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    const wrongPassword = randomTestValue("wrong-secret");
    const newUsername = randomTestValue("rejected-user");
    const newPassword = randomTestValue("rejected-secret");

    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(wrongPassword);
    await form.locator('input[autocomplete="username"]').nth(1).fill(newUsername);
    await form.locator('input[autocomplete="new-password"]').fill(newPassword);
    await form.locator(".setup-save-btn").click();

    await expect(form.locator(".auth-credentials-error")).toBeVisible();
    await expect(form.locator(".auth-credentials-status")).not.toBeVisible();

    // Log out fully (cookie + localStorage bearer token; see comment above).
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });

    // The original credentials must still work: the rejected submission
    // must not have mutated the credential store.
    await page.goto(backend.baseURL);
    await loginViaUI(page, backend);

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });

  test("rotates only the password, keeping the same username", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    const newPassword = randomTestValue("samename-secret");

    // The "new username" input is `required` (AuthCredentialsSetting.tsx),
    // so a same-username-only change must re-enter the current username
    // there rather than leaving the field blank.
    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(backend.password);
    await form.locator('input[autocomplete="username"]').nth(1).fill(backend.username);
    await form.locator('input[autocomplete="new-password"]').fill(newPassword);
    await form.locator(".setup-save-btn").click();

    await expect(form.locator(".auth-credentials-status")).toBeVisible();
    await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

    // Log out fully (cookie + localStorage bearer token; see comment above).
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });

    // The old password must now be rejected, even with the (unchanged) username.
    await page.goto(backend.baseURL);
    await page.locator('input[autocomplete="username"]').fill(backend.username);
    await page.locator('input[autocomplete="current-password"]').fill(backend.password);
    await page.locator(".login-submit").click();
    await expect(page.locator(".login-error")).toBeVisible();
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();

    // The same username with the new password must work.
    await page.goto(backend.baseURL);
    await loginViaUI(page, { ...backend, password: newPassword });

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });
});
