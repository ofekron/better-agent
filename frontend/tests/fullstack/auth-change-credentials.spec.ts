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

  test("rejects an empty new password, client-side and server-side, and leaves credentials unchanged", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    const newUsername = randomTestValue("emptypw-user");

    // The new-password input is `required` (AuthCredentialsSetting.tsx), so
    // the browser's own constraint validation must block the submit event
    // before the component's fetch ever fires — leave it blank and confirm
    // neither a status nor an error appears (i.e. no request was made).
    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(backend.password);
    await form.locator('input[autocomplete="username"]').nth(1).fill(newUsername);
    await form.locator(".setup-save-btn").click();

    await expect(form.locator(".auth-credentials-status")).not.toBeVisible();
    await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

    // Bypass the client-side `required` guard entirely and hit the real
    // endpoint directly, riding the same session cookie the page holds, to
    // prove the server itself rejects an empty new_password
    // (auth_routes.change_credentials: `if not new_username or not
    // body.new_password: raise HTTPException(400, ...)`).
    const direct = await page.request.post(`${backend.baseURL}/api/auth/change_credentials`, {
      data: {
        current_username: backend.username,
        current_password: backend.password,
        new_username: newUsername,
        new_password: "",
      },
    });
    expect(direct.status()).toBe(400);

    // Neither attempt may have mutated the credential store: the original
    // credentials must still log in through the real form.
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });
    await page.goto(backend.baseURL);
    await loginViaUI(page, backend);

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });

  test("accepts an HTML/script-injection-shaped username and renders it as inert text", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    // ChangeCredentialsBody.new_username (backend/auth_routes.py) is an
    // untyped `str` with no length/character validation server-side (only
    // `.strip()` + non-empty), so this is accepted verbatim. The frontend
    // never renders usernames via dangerouslySetInnerHTML/innerHTML
    // (App.tsx's .sidebar-user-name span, MessageBubble.tsx headers all use
    // plain JSX text interpolation), so React must auto-escape it into
    // inert text rather than live markup.
    const newUsername = "<script>alert(1)</script>";
    const newPassword = randomTestValue("xss-secret");

    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(backend.password);
    await form.locator('input[autocomplete="username"]').nth(1).fill(newUsername);
    await form.locator('input[autocomplete="new-password"]').fill(newPassword);
    await form.locator(".setup-save-btn").click();

    await expect(form.locator(".auth-credentials-status")).toBeVisible();
    await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

    // Log out fully (cookie + localStorage bearer token; see comment above).
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });

    // Log back in with the new, weird username. loginViaUI already waits
    // for the composer textarea to be visible, which proves the app shell
    // booted normally (no injected script broke rendering).
    await page.goto(backend.baseURL);
    await loginViaUI(page, { ...backend, username: newUsername, password: newPassword });

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(newUsername);

    // The username must appear as plain, inert text — not be interpreted
    // as markup by the DOM.
    await expect(page.locator(".sidebar-user-name")).toHaveText(newUsername);
    await expect(page.locator('script:has-text("alert(1)")')).toHaveCount(0);
  });

  test("submits via Enter key in the last field, not just the save button", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    await page.goto(`${backend.baseURL}/settings`);
    await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

    const newPassword = randomTestValue("enter-key-secret");

    // AuthCredentialsSetting.tsx renders a real <form onSubmit=...> with a
    // <button type="submit">, so pressing Enter in a field triggers the
    // browser's native form submission (no click on .setup-save-btn here) -
    // a basic keyboard-accessibility check.
    const form = page.locator(".auth-credentials-setting");
    await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
    await form.locator('input[autocomplete="current-password"]').fill(backend.password);
    await form.locator('input[autocomplete="username"]').nth(1).fill(backend.username);
    await form.locator('input[autocomplete="new-password"]').fill(newPassword);
    await form.locator('input[autocomplete="new-password"]').press("Enter");

    await expect(form.locator(".auth-credentials-status")).toBeVisible();
    await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

    // Log out fully (cookie + localStorage bearer token; see comment above).
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });

    // The new password must work, through the real login form.
    await page.goto(backend.baseURL);
    await loginViaUI(page, { ...backend, password: newPassword });

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });

  // auth_routes.change_credentials calls the SAME auth.rate_limit_check(ip)
  // as /login (auth.py's sliding-window limiter, _RL_WINDOW = 300s,
  // _RL_MAX = 5), keyed only by source IP — so repeated wrong-current-
  // password attempts here get the same real 429 lock-out proven for
  // /login in auth-login.spec.ts. Drives that directly against the
  // endpoint, riding the real session cookie from a real UI login.
  test("rate-limits repeated wrong-current-password change attempts with a 429", async ({
    page,
    backend,
  }) => {
    await loginViaUI(page, backend);

    const changeUrl = `${backend.baseURL}/api/auth/change_credentials`;
    const attempt = () =>
      page.request.post(changeUrl, {
        data: {
          current_username: backend.username,
          current_password: "definitely-wrong",
          new_username: randomTestValue("ratelimited-user"),
          new_password: randomTestValue("ratelimited-secret"),
        },
        failOnStatusCode: false,
      });

    const statuses: number[] = [];
    let res = await attempt();
    statuses.push(res.status());
    while (res.status() !== 429 && statuses.length < 20) {
      res = await attempt();
      statuses.push(res.status());
    }

    // _RL_MAX (5) attempts are admitted (wrong current password → 401)
    // before the limiter kicks in on the 6th request within the window.
    expect(statuses).toEqual([401, 401, 401, 401, 401, 429]);
    expect((await res.json()).detail).toBe("too many attempts");

    // The lock-out must not have mutated the credential store: the real
    // (correct) credentials still work through the real login form.
    await page.evaluate(async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      localStorage.clear();
    });
    await page.goto(backend.baseURL);
    await loginViaUI(page, backend);

    const me = await page.evaluate(async () => {
      const res = await fetch("/api/auth/me", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(me.status).toBe(200);
    expect(me.body.username).toBe(backend.username);
  });

  // auth_routes.change_credentials calls auth_secrets.write_login_credentials,
  // which only overwrites the username/password_hash keychain entries. Only
  // the FIRST-run write_credentials mints a fresh session_secret; a regular
  // credential change leaves it untouched, and auth.reload_credentials()
  // re-reads the same secret. Bearer tokens (auth.create_token /
  // create_access_token) are itsdangerous-signed with that session secret
  // and carry only `{"username": ...}` — no password-hash-derived
  // component — so they're invalidated by secret rotation or expiry only,
  // never by a plain password change. This proves that against the real
  // QR flow (auth-login.spec.ts's grant/redeem), whose access token is the
  // purely bearer-token-based (no cookie) session per qr_redeem's own
  // docstring ("No cookie is set").
  test("a QR-redeemed bearer token keeps authenticating after the password changes in a different session", async ({
    page,
    browser,
    backend,
  }) => {
    // Session A: real cookie-based login via the UI.
    await loginViaUI(page, backend);

    // Mint a QR grant from session A and redeem it into a brand-new browser
    // context (session B), exactly like auth-login.spec.ts's QR test.
    const grantRes = await page.request.get(`${backend.baseURL}/api/auth/qr_grant`);
    expect(grantRes.ok()).toBe(true);
    const { login_url } = await grantRes.json();
    const grant = new URL(login_url).searchParams.get("qr");
    expect(grant).toBeTruthy();

    const sessionB = await browser.newContext();
    try {
      const redeemRes = await sessionB.request.post(`${backend.baseURL}/api/auth/qr_redeem`, {
        data: { grant },
      });
      expect(redeemRes.ok()).toBe(true);
      const { access_token } = await redeemRes.json();
      expect(access_token).toBeTruthy();

      const meBeforeChange = await sessionB.request.get(`${backend.baseURL}/api/auth/me`, {
        headers: { Authorization: `Bearer ${access_token}` },
      });
      expect(meBeforeChange.status()).toBe(200);
      expect((await meBeforeChange.json()).username).toBe(backend.username);

      // Change the password through session A's real Settings > Account form.
      await page.goto(`${backend.baseURL}/settings`);
      await page.locator(".settings-page-nav button", { hasText: "Account" }).click();

      const newPassword = randomTestValue("qr-survives-secret");
      const form = page.locator(".auth-credentials-setting");
      await form.locator('input[autocomplete="username"]').nth(0).fill(backend.username);
      await form.locator('input[autocomplete="current-password"]').fill(backend.password);
      await form.locator('input[autocomplete="username"]').nth(1).fill(backend.username);
      await form.locator('input[autocomplete="new-password"]').fill(newPassword);
      await form.locator(".setup-save-btn").click();

      await expect(form.locator(".auth-credentials-status")).toBeVisible();
      await expect(form.locator(".auth-credentials-error")).not.toBeVisible();

      // Session B's bearer token was never tied to the password hash, so
      // it must still authenticate (still within its 15-minute TTL).
      const meAfterChange = await sessionB.request.get(`${backend.baseURL}/api/auth/me`, {
        headers: { Authorization: `Bearer ${access_token}` },
      });
      expect(meAfterChange.status()).toBe(200);
      expect((await meAfterChange.json()).username).toBe(backend.username);
    } finally {
      await sessionB.close();
    }
  });
});
