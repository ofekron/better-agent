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
});
