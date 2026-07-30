import { test, expect } from "./harness/fixtures";
import { openProviderSettings, pickCustomSelectOption, saveProviderSettings } from "./harness/settings";

// Provider Config Sync (cross-CLI capability sync) lives in a separate
// private-repo extension, not in this codebase. This tests the actual
// in-app analog: per-provider capability overrides in Settings > Providers
// > edit, round-tripped through a real PATCH /api/providers/{id} and a
// real page reload (not just in-memory React state).
test("a capability override persists across a real reload", async ({ authedPage: page, backend }) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "capability-override-select-supports_fork", "Off");
  await saveProviderSettings(page);

  // openProviderSettings does a real page.goto (full navigation, not
  // client-side routing), so re-opening here proves the override survived
  // a real reload, not just in-memory React state.
  await openProviderSettings(page, backend.baseURL, "claude");

  await expect(page.getByTestId("capability-override-select-supports_fork")).toHaveText(/off/i);
});

// Resetting to "Inherit (default)" must actually delete the persisted
// override, not just leave the UI showing "off". The backend stores
// overrides in a `capabilities` dict (PATCH request field) that is
// returned as `capability_overrides` (GET response field); "inherit" is
// represented by the key being ABSENT from that dict, never by a stored
// `false`/`null`. This drives the override to "Off" and back to
// "Inherit (default)" through real PATCH calls and reloads, and checks
// both the outgoing PATCH body and a fresh GET /api/providers response to
// prove the key is actually gone server-side.
test("resetting a capability override to Inherit clears the persisted override", async ({
  authedPage: page,
  backend,
}) => {
  const providersRes = await page.request.get(`${backend.baseURL}/api/providers`);
  const providers = (await providersRes.json()) as Array<{ id: string; kind: string }>;
  const providerId = providers.find((p) => p.kind === "claude")?.id;
  expect(providerId).toBeTruthy();

  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "capability-override-select-supports_fork", "Off");
  await saveProviderSettings(page);

  await openProviderSettings(page, backend.baseURL, "claude");
  await expect(page.getByTestId("capability-override-select-supports_fork")).toHaveText(/off/i);

  const patchRequest = page.waitForRequest(
    (req) =>
      new URL(req.url()).pathname === `/api/providers/${providerId}` && req.method() === "PATCH",
  );
  await pickCustomSelectOption(page, "capability-override-select-supports_fork", "Inherit (default)");
  await saveProviderSettings(page);

  const req = await patchRequest;
  const body = req.postDataJSON() as { capabilities?: Record<string, boolean> };
  expect(body.capabilities).not.toHaveProperty("supports_fork");

  const afterRes = await page.request.get(`${backend.baseURL}/api/providers`);
  const afterProviders = (await afterRes.json()) as Array<{
    id: string;
    capability_overrides?: Record<string, boolean>;
  }>;
  const afterProvider = afterProviders.find((p) => p.id === providerId);
  expect(afterProvider?.capability_overrides ?? {}).not.toHaveProperty("supports_fork");

  await openProviderSettings(page, backend.baseURL, "claude");
  await expect(page.getByTestId("capability-override-select-supports_fork")).toHaveText(/inherit/i);
});

// Setting several different capability overrides in the same edit session
// must persist ALL of them independently on a single save — no cross-key
// bleed (e.g. two keys landing on the same value) and no clobbering of keys
// that were never touched (which must remain "Inherit (default)").
test("multiple capability overrides set together all persist independently", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");

  await pickCustomSelectOption(page, "capability-override-select-supports_fork", "Off");
  await pickCustomSelectOption(page, "capability-override-select-supports_manager_mode", "On");
  await pickCustomSelectOption(page, "capability-override-select-supports_rewind", "Off");
  await saveProviderSettings(page);

  await openProviderSettings(page, backend.baseURL, "claude");

  await expect(page.getByTestId("capability-override-select-supports_fork")).toHaveText(/off/i);
  await expect(page.getByTestId("capability-override-select-supports_manager_mode")).toHaveText(/on/i);
  await expect(page.getByTestId("capability-override-select-supports_rewind")).toHaveText(/off/i);
  await expect(page.getByTestId("capability-override-select-supports_steering")).toHaveText(/inherit/i);
});
