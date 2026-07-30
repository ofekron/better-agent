import { test, expect } from "./harness/fixtures";

// The extension-catalog browse/install UI (frontend/src/components/ExtensionSlots.tsx)
// only exists inside an iframe-hosted marketplace extension, reachable via a
// postMessage bridge with no top-level page for Playwright to click through.
// So this drives the real REST surface directly with `authedPage.request`
// (an APIRequestContext that shares the authenticated cookie jar with the
// page once `authedPage` has logged in) against the real running backend:
// backend/extension_api.py's `/api/extensions/marketplace/*` routes, which
// delegate to the bundled `ofek-dev.marketplace` extension's own backend
// (extensions/marketplace/backend/routes.py).
//
// Full install/uninstall round trip is NOT exercised here. Verified by
// probing a real, freshly provisioned backend directly (twice,
// deterministically): every one of these endpoints requires a genuine
// marketplace login before it will preview or install anything —
// `preview_marketplace_extension` always calls the marketplace extension's
// own `/metadata/{id}` route, which unconditionally calls
// `_require_access_token()` (extensions/marketplace/backend/routes.py) —
// and a fresh isolated test backend has no stored marketplace OAuth
// session and no way to acquire one without a real account on the external
// marketplace service. There is also no catalog-list endpoint reachable
// without one: `GET /api/extensions/ofek-dev.marketplace/backend/catalog`
// 500s on every normally-provisioned install, because the bundled
// marketplace extension's own installed source record has an empty
// `repo_url` (see extension_store.py's `_install_public_package_snapshot`),
// which the route's "local dev catalog" fallback requires to be non-empty
// (routes.py `_private_repo_root`). That is a real product bug, tracked
// separately — it is not something this test should route around with a
// mock.
//
// What IS real, deterministic, and testable without any external
// dependency is the fail-closed contract of every mutating marketplace
// endpoint: they refuse to act without a marketplace login, and they
// refuse to touch an extension that isn't marketplace-managed. That is
// what these tests lock in.
test.describe("extension marketplace catalog", () => {
  // `ofek-dev.ask` ships bundled with every install (backend/extension_store.py
  // BUILTIN_ASK_EXTENSION_ID) with source.type "better_agent_bundled" —
  // never through the marketplace — so it is a stable, always-present,
  // non-marketplace-managed extension id to probe the marketplace endpoints
  // against.
  const NON_MARKETPLACE_EXTENSION_ID = "ofek-dev.ask";
  const UNKNOWN_EXTENSION_ID = "ofek-dev.does-not-exist";

  test.beforeEach(async ({ authedPage, backend }) => {
    // GET /api/extensions triggers extension_store's required-extension
    // reconciliation as a side effect (it seeds the bundled marketplace
    // and ask extensions on first call). Without this, the marketplace
    // endpoints below 404 with "Extension has no backend surface" /
    // "Extension not installed" instead of exercising the real
    // auth/ownership checks this suite is about — reproduced directly
    // against a real backend.
    const res = await authedPage.request.get(`${backend.baseURL}/api/extensions`);
    expect(res.ok()).toBe(true);
  });

  test("preview refuses without a marketplace login", async ({ authedPage, backend }) => {
    const res = await authedPage.request.post(
      `${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/preview`,
    );
    expect(res.status()).toBe(401);
    expect((await res.json()).detail).toBe("marketplace login required");
  });

  test("install rejects a malformed preview token", async ({ authedPage, backend }) => {
    const res = await authedPage.request.post(
      `${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/install`,
      { data: { preview_token: "not-a-real-token", entitlement_token: "" } },
    );
    // Pydantic's strict 32-char hex pattern on `preview_token` rejects this
    // before the request ever reaches extension_store — no preview was ever
    // issued for this id, so no legitimate token could exist.
    expect(res.status()).toBe(422);
  });

  test("enabled/uninstall refuse an extension that is not marketplace-managed, without mutating it", async ({
    authedPage,
    backend,
  }) => {
    const patchRes = await authedPage.request.patch(
      `${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/enabled`,
      { data: { enabled: false } },
    );
    expect(patchRes.status()).toBe(400);
    expect((await patchRes.json()).detail).toBe("Extension is not managed by marketplace");

    const deleteRes = await authedPage.request.delete(
      `${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}`,
    );
    expect(deleteRes.status()).toBe(400);
    expect((await deleteRes.json()).detail).toBe("Extension is not managed by marketplace");

    // Neither rejected call had any side effect: the extension is
    // untouched in the real installed-extensions projection (the same one
    // frontend/src/components/SettingsPage.tsx's ExtensionUiSettingsSection
    // reads from).
    const listRes = await authedPage.request.get(`${backend.baseURL}/api/extensions`);
    const { extensions } = (await listRes.json()) as {
      extensions: Array<{ manifest: { id: string }; enabled: boolean; source: { type: string } }>;
    };
    const ask = extensions.find((item) => item.manifest.id === NON_MARKETPLACE_EXTENSION_ID);
    expect(ask?.enabled).toBe(true);
    expect(ask?.source.type).toBe("better_agent_bundled");
  });

  test("enabled/uninstall fail closed on an extension id that isn't installed at all", async ({
    authedPage,
    backend,
  }) => {
    const patchRes = await authedPage.request.patch(
      `${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}/enabled`,
      { data: { enabled: true } },
    );
    expect(patchRes.status()).toBe(400);
    expect((await patchRes.json()).detail).toBe("Extension not installed");

    const deleteRes = await authedPage.request.delete(
      `${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}`,
    );
    expect(deleteRes.status()).toBe(400);
    expect((await deleteRes.json()).detail).toBe("Extension not installed");
  });

  test("preview/enabled reject path-traversal and malformed ids without ever escaping the extension registry", async ({
    authedPage,
    backend,
  }) => {
    // A literal `../` id is collapsed by the HTTP client's own URL
    // dot-segment normalization BEFORE the request is sent — `.../marketplace/
    // ../../../etc/passwd/preview` resolves to `/etc/passwd/preview`, a path
    // outside `/api/` entirely. That falls through to `main.py`'s
    // `mount_frontend` StaticFiles mount, which only serves GET/HEAD and
    // rejects every other method with 405 before any extension_id-aware code
    // ever runs. Reproduced directly against a real backend with both a
    // normalized client and curl's `--path-as-is` (raw, unnormalized
    // request-line) — both 405, so this holds however the HTTP layer treats
    // the dots.
    const RAW_TRAVERSAL_ID = "../../../etc/passwd";
    // `%2F` is untouched by dot-segment collapsing (only literal
    // `/`-delimited segments are processed), so this string survives the
    // client as one opaque path component. Uvicorn's ASGI layer then
    // percent-decodes the whole path before Starlette's router matches it,
    // turning `%2F` back into `/` — producing extra path segments that don't
    // fit the single-segment `{extension_id}` converter, so routing itself
    // rejects it the same way (405), never reaching a handler.
    const ENCODED_TRAVERSAL_ID = "..%2F..%2Fetc%2Fpasswd";

    for (const travId of [RAW_TRAVERSAL_ID, ENCODED_TRAVERSAL_ID]) {
      const previewRes = await authedPage.request.post(
        `${backend.baseURL}/api/extensions/marketplace/${travId}/preview`,
      );
      expect(previewRes.status()).toBe(405);

      const enabledRes = await authedPage.request.patch(
        `${backend.baseURL}/api/extensions/marketplace/${travId}/enabled`,
        { data: { enabled: false } },
      );
      expect(enabledRes.status()).toBe(405);
    }

    // A null-byte-style id has no literal "/", so it stays one path segment
    // and DOES reach the real `{extension_id}` handlers — this is the case
    // that proves the handlers themselves treat an unrecognized/unusual id
    // as an inert string (dict/registry lookup miss) rather than doing
    // anything filesystem- or auth-bypass-unsafe with it.
    const WEIRD_ID = "null%00byte";

    const weirdEnabledRes = await authedPage.request.patch(
      `${backend.baseURL}/api/extensions/marketplace/${WEIRD_ID}/enabled`,
      { data: { enabled: false } },
    );
    // Same `_require_extension_source` dict lookup as the UNKNOWN_EXTENSION_ID
    // case above — the raw id string (with its embedded %00) simply isn't a
    // key in the installed-extensions map.
    expect(weirdEnabledRes.status()).toBe(400);
    expect((await weirdEnabledRes.json()).detail).toBe("Extension not installed");

    // preview_marketplace_extension always calls the marketplace extension's
    // own `/metadata/{id}` route first, which unconditionally requires a
    // stored marketplace access token before it ever looks at the id
    // (extensions/marketplace/backend/routes.py `_require_access_token`) —
    // so, like the "preview refuses without a marketplace login" test above,
    // this fails closed without any id-specific logic running. It must never
    // return 2xx; the exact non-2xx status can vary with how fast the local
    // credential-store lookup resolves, so this asserts the safety contract
    // (closed, and a real handled-error body) rather than one fixed code.
    const weirdPreviewRes = await authedPage.request.post(
      `${backend.baseURL}/api/extensions/marketplace/${WEIRD_ID}/preview`,
    );
    expect(weirdPreviewRes.ok()).toBe(false);
    const weirdPreviewBody = await weirdPreviewRes.json();
    expect(typeof weirdPreviewBody.detail).toBe("string");

    // No side effects from any of the above: the catalog projection is still
    // healthy and reachable.
    const listRes = await authedPage.request.get(`${backend.baseURL}/api/extensions`);
    expect(listRes.ok()).toBe(true);
  });

  test("concurrent marketplace requests stay well-formed and leave installed-extension state consistent", async ({
    authedPage,
    backend,
  }) => {
    // Fire a mixed batch of preview/enabled/delete calls against both a
    // real, non-marketplace-managed extension and an unknown one,
    // concurrently. None of these should ever 2xx (see the tests above),
    // but under concurrent load the goal here is different: every response
    // must still be a well-formed HTTP response — not a connection reset,
    // timeout, or an unhandled-exception 500 with no JSON body — and the
    // installed-extensions projection must come out the other side
    // unmutated.
    const requests = [
      () => authedPage.request.post(`${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/preview`),
      () => authedPage.request.patch(`${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/enabled`, { data: { enabled: false } }),
      () => authedPage.request.delete(`${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}`),
      () => authedPage.request.post(`${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}/preview`),
      () => authedPage.request.patch(`${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}/enabled`, { data: { enabled: true } }),
      () => authedPage.request.delete(`${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}`),
      () => authedPage.request.post(`${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/preview`),
      () => authedPage.request.patch(`${backend.baseURL}/api/extensions/marketplace/${UNKNOWN_EXTENSION_ID}/enabled`, { data: { enabled: false } }),
    ];

    const responses = await Promise.all(requests.map((fire) => fire()));

    for (const res of responses) {
      // A crash or connection error throws inside Playwright's request API
      // rather than resolving, so simply reaching this point with a status
      // code already rules that out. The body must also still be the
      // handled-error JSON shape every synchronous variant of these calls
      // returns above, not an empty/broken response from a half-crashed
      // handler.
      expect(res.status()).toBeGreaterThanOrEqual(400);
      const body = await res.json();
      expect(typeof body.detail).toBe("string");
    }

    // The real installed-extensions projection is untouched: the
    // non-marketplace extension is still enabled with its original,
    // non-marketplace source type.
    const listRes = await authedPage.request.get(`${backend.baseURL}/api/extensions`);
    expect(listRes.status()).toBe(200);
    const { extensions } = (await listRes.json()) as {
      extensions: Array<{ manifest: { id: string }; enabled: boolean; source: { type: string } }>;
    };
    const ask = extensions.find((item) => item.manifest.id === NON_MARKETPLACE_EXTENSION_ID);
    expect(ask?.enabled).toBe(true);
    expect(ask?.source.type).toBe("better_agent_bundled");
  });

  test("marketplace endpoints reject completely unauthenticated requests before any marketplace-specific check", async ({
    backend,
    playwright,
  }) => {
    // A brand-new APIRequestContext with no cookie jar and no bearer token
    // at all — deliberately not `authedPage.request`, which carries the real
    // logged-in session cookie. This proves the outer app-auth gate (the
    // dependency every `/api/*` route sits behind) runs and rejects BEFORE
    // any marketplace-specific "no marketplace login" logic gets a chance
    // to run — those 401s asserted elsewhere in this file are a different,
    // later failure mode that only fires once the caller is already an
    // authenticated app user.
    const anonymous = await playwright.request.newContext({ baseURL: backend.baseURL });
    try {
      const previewRes = await anonymous.post(
        `/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/preview`,
      );
      expect(previewRes.status()).toBe(401);

      const listRes = await anonymous.get(`/api/extensions`);
      expect(listRes.status()).toBe(401);
    } finally {
      await anonymous.dispose();
    }
  });

  test("preview has no rate-limiting, so a burst of repeated requests must still not fall over", async ({
    authedPage,
    backend,
  }) => {
    // Unlike backend/auth.py's login endpoint (`_RL_MAX` / `_RL_WINDOW`,
    // a sliding-window lock-out per IP), none of the marketplace routes
    // in backend/extension_api.py or the marketplace extension's own
    // extensions/marketplace/backend/routes.py implement any local
    // rate-limiting. The only 429 in that code
    // (extensions/marketplace/backend/routes.py's `_server_request`) is a
    // passthrough of the *remote* marketplace server's own response, and
    // it's unreachable here: `preview_marketplace_extension` always calls
    // `_require_access_token()` first (routes.py), which 401s from local
    // credential-store state alone, before any network call is made. So
    // there is no real rate-limit contract to prove for this route today.
    //
    // What's tested instead is the honest fallback: that the handler is
    // cheap and stateless enough to take a real burst of sequential
    // requests without degrading — no crash, no non-JSON/broken response,
    // and no runaway latency growth (which would point at something like
    // an unbounded per-request allocation or a lock held too long).
    const BURST_SIZE = 20;
    const latenciesMs: number[] = [];

    for (let i = 0; i < BURST_SIZE; i += 1) {
      const start = Date.now();
      const res = await authedPage.request.post(
        `${backend.baseURL}/api/extensions/marketplace/${NON_MARKETPLACE_EXTENSION_ID}/preview`,
      );
      latenciesMs.push(Date.now() - start);

      expect(res.status()).toBe(401);
      expect((await res.json()).detail).toBe("marketplace login required");
    }

    // No ever-growing latency: compare the mean of the first half against
    // the mean of the second half of the burst. A real leak (e.g. an
    // unbounded list/dict growing per call) would show up as the back
    // half taking substantially longer than the front half. A generous
    // multiplier avoids flaking on ordinary scheduling jitter while still
    // catching genuine unbounded growth.
    const half = BURST_SIZE / 2;
    const mean = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length;
    const firstHalfMean = mean(latenciesMs.slice(0, half));
    const secondHalfMean = mean(latenciesMs.slice(half));
    expect(secondHalfMean).toBeLessThan(Math.max(firstHalfMean * 4, firstHalfMean + 200));

    // The backend is still healthy and the projection is unmutated after
    // the burst.
    const listRes = await authedPage.request.get(`${backend.baseURL}/api/extensions`);
    expect(listRes.status()).toBe(200);
    const { extensions } = (await listRes.json()) as {
      extensions: Array<{ manifest: { id: string }; enabled: boolean }>;
    };
    const ask = extensions.find((item) => item.manifest.id === NON_MARKETPLACE_EXTENSION_ID);
    expect(ask?.enabled).toBe(true);
  });
});
