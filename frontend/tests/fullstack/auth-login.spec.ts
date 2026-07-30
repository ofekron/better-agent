import { readFileSync } from "node:fs";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import { loginViaUI } from "./harness/login";
import { FRONTEND_DIR } from "./harness/paths";

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

  // auth.py's sliding-window limiter (_RL_WINDOW = 300s, _RL_MAX = 5) counts
  // attempts per source IP, not failures, so the 6th POST within the window
  // is rejected before credentials are even checked. Drives that against
  // the real limiter, then confirms Login.tsx surfaces it through the same
  // `login.tooManyAttempts` i18n key it uses for the real 429 response.
  test("rate-limits repeated failed login attempts with a 429", async ({ page, backend }) => {
    const loginUrl = `${backend.baseURL}/api/auth/login`;
    const attempt = () =>
      page.request.post(loginUrl, {
        data: { username: backend.username, password: "definitely-wrong" },
        failOnStatusCode: false,
      });

    const statuses: number[] = [];
    let res = await attempt();
    statuses.push(res.status());
    while (res.status() !== 429 && statuses.length < 20) {
      res = await attempt();
      statuses.push(res.status());
    }

    // _RL_MAX (5) attempts are admitted (wrong password → 401) before the
    // limiter kicks in on the 6th request within the _RL_WINDOW (300s).
    expect(statuses).toEqual([401, 401, 401, 401, 401, 429]);
    expect((await res.json()).detail).toBe("too many attempts");

    // The real Login.tsx form, hitting the backend from the same source IP,
    // must be locked out too (even with the correct password) and show the
    // exact string behind the `login.tooManyAttempts` i18n key.
    const enStrings = JSON.parse(
      readFileSync(path.join(FRONTEND_DIR, "src/i18n/en.json"), "utf-8")
    );
    await page.goto(backend.baseURL);
    await page.locator('input[autocomplete="username"]').fill(backend.username);
    await page.locator('input[autocomplete="current-password"]').fill(backend.password);
    await page.locator(".login-submit").click();
    await expect(page.locator(".login-error")).toHaveText(enStrings["login.tooManyAttempts"]);
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();
  });

  // auth_routes.py's session is a Starlette SessionMiddleware cookie: the
  // full {"user": {...}} payload is signed and stored client-side in the
  // `better_agent_session` cookie itself (see main.py's SessionMiddleware
  // wiring), and auth_routes.py's /api/auth/logout just does
  // `request.session.clear()` on the requesting client's own cookie. There
  // is no server-side session table, so two browser contexts each hold an
  // independent signed cookie and logging one out cannot touch the other.
  test("two independent browser sessions stay isolated on logout", async ({
    browser,
    backend,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    try {
      const pageA = await contextA.newPage();
      const pageB = await contextB.newPage();

      await loginViaUI(pageA, backend);
      await loginViaUI(pageB, backend);

      const meA = await pageA.evaluate(async () => {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        return { status: res.status, body: await res.json() };
      });
      const meB = await pageB.evaluate(async () => {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        return { status: res.status, body: await res.json() };
      });
      expect(meA.status).toBe(200);
      expect(meA.body.username).toBe(backend.username);
      expect(meB.status).toBe(200);
      expect(meB.body.username).toBe(backend.username);

      // Log out context A only.
      const logoutStatus = await pageA.evaluate(async () => {
        const res = await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
        return res.status;
      });
      expect(logoutStatus).toBe(204);

      const meAAfterLogout = await pageA.evaluate(async () => {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        return res.status;
      });
      expect(meAAfterLogout).toBe(401);

      // Context B's independent session cookie must be unaffected.
      const meBAfterLogout = await pageB.evaluate(async () => {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        return { status: res.status, body: await res.json() };
      });
      expect(meBAfterLogout.status).toBe(200);
      expect(meBAfterLogout.body.username).toBe(backend.username);
    } finally {
      await contextA.close();
      await contextB.close();
    }
  });
});
