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

  // Login.tsx's scan-to-login path: an authenticated session mints a
  // one-time grant via GET /api/auth/qr_grant (loopback-or-authed gated in
  // auth_routes.py); a phone (here, a fresh unauthenticated browser
  // context) opens .../?qr=<grant>, Login.tsx auto-redeems it via
  // POST /api/auth/qr_redeem for a bearer token pair, and no password is
  // ever typed there. qr_auth.consume_grant is single-use, so redeeming
  // the same grant again must fail with 401.
  test("QR scan-to-login redeems a real grant exactly once, no password typed", async ({
    page,
    browser,
    backend,
  }) => {
    await loginViaUI(page, backend);

    const grantRes = await page.request.get(`${backend.baseURL}/api/auth/qr_grant`);
    expect(grantRes.ok()).toBe(true);
    const { login_url } = await grantRes.json();
    const grant = new URL(login_url).searchParams.get("qr");
    expect(grant).toBeTruthy();

    const scanContext = await browser.newContext();
    try {
      const scanPage = await scanContext.newPage();
      await scanPage.goto(login_url);
      await scanPage.getByTestId("input-textarea").waitFor({ state: "visible", timeout: 15_000 });

      // Reached the app shell without ever seeing (let alone filling) a
      // password field in this fresh, unauthenticated context.
      await expect(scanPage.locator('input[autocomplete="current-password"]')).toHaveCount(0);

      const me = await scanPage.evaluate(async () => {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        return { status: res.status, body: await res.json() };
      });
      expect(me.status).toBe(200);
      expect(me.body.username).toBe(backend.username);

      // The grant is one-time: redeeming it again must be rejected.
      const replay = await scanPage.request.post(`${backend.baseURL}/api/auth/qr_redeem`, {
        data: { grant },
        failOnStatusCode: false,
      });
      expect(replay.status()).toBe(401);
    } finally {
      await scanContext.close();
    }
  });

  // The signed session payload lives entirely in the `better_agent_session`
  // cookie (Starlette SessionMiddleware, see main.py's
  // `session_cookie="better_agent_session"` wiring) — there is no
  // server-side session table. Tampering with the cookie's value must fail
  // the itsdangerous signature check server-side and bounce the SPA back to
  // the login screen on reload, not leave it half-authed or crashed.
  test("rejects a tampered session cookie and returns to the login screen", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    const cookiesBefore = await page.context().cookies();
    const sessionCookie = cookiesBefore.find((c) => c.name === "better_agent_session");
    expect(sessionCookie).toBeTruthy();

    const meBefore = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(meBefore).toBe(200);

    await page.context().addCookies([
      {
        ...sessionCookie!,
        value: "tampered.garbage.value",
      },
    ]);

    const meAfterTamper = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(meAfterTamper).toBe(401);

    await page.reload();
    await expect(page.locator(".login-submit")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();
  });

  // Drives the real UI logout control (App.tsx's `handleLogout`, wired to
  // the sidebar's `.sidebar-logout-btn`) rather than the raw
  // POST /api/auth/logout used for cleanup elsewhere in this file.
  // handleLogout POSTs the logout, clears any stored bearer token, and
  // reloads so the top-level App re-mounts and re-runs its auth check —
  // this confirms that whole real flow ends both server-side (401 on
  // /api/auth/me) and client-side (Login screen re-rendered).
  test("the real UI logout button signs the user out and returns to the login screen", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    const meBeforeLogout = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(meBeforeLogout).toBe(200);

    await page.locator(".sidebar-logout-btn").click();

    await expect(page.locator(".login-submit")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("input-textarea")).not.toBeVisible();

    const meAfterLogout = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(meAfterLogout).toBe(401);
  });

  // Unlike the dual-CONTEXT isolation test above, two tabs in the SAME
  // context share one cookie jar, so they share the very same
  // `better_agent_session` cookie. Logging out through the real UI in tab 1
  // clears that one shared cookie server-side, so tab 2 must also see
  // itself as unauthenticated on its next check — no independent session to
  // protect it.
  test("two tabs in the same browser context both lose auth after a real logout in one", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    const page2 = await page.context().newPage();

    const me1 = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    const me2 = await page2.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(me1).toBe(200);
    expect(me2).toBe(200);

    await page.locator(".sidebar-logout-btn").click();
    await expect(page.locator(".login-submit")).toBeVisible({ timeout: 15_000 });

    await page2.reload();
    await expect(page2.locator(".login-submit")).toBeVisible({ timeout: 15_000 });
    await expect(page2.getByTestId("input-textarea")).not.toBeVisible();

    const me2AfterLogout = await page2.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return res.status;
    });
    expect(me2AfterLogout).toBe(401);

    await page2.close();
  });
});
