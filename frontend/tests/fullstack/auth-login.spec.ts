import { test, expect } from "./harness/fixtures";
import { loginViaUI } from "./harness/login";

// Validates the full-stack harness itself: a real backend subprocess with a
// randomly generated, argon2id-hashed password (headless-auth), driving the
// REAL Login.tsx form (not a token bypass) through a real Chromium browser.
test.describe("real login", () => {
  test("logs in with the real generated credentials and reaches the app shell", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);
    await expect(page.getByTestId("input-textarea")).toBeVisible();

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });

  test("rejects the wrong password", async ({ page, backend }) => {
    await page.goto(backend.baseURL);
    await page.locator('input[autocomplete="username"]').fill(backend.username);
    await page.locator('input[autocomplete="current-password"]').fill("definitely-wrong");
    await page.locator(".login-submit").click();
    await expect(page.locator(".login-error")).toBeVisible();
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();
  });
});
