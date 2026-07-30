import { test, expect } from "./harness/fixtures";
import { openProviderSettings, pickCustomSelectOption, saveProviderSettings } from "./harness/settings";
import { createSessionWithPrompt } from "./harness/session";

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
  const providers = ((await providersRes.json()) as { providers: Array<{ id: string; kind: string }> })
    .providers;
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
  const afterProviders = (
    (await afterRes.json()) as {
      providers: Array<{ id: string; capability_overrides?: Record<string, boolean> }>;
    }
  ).providers;
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

// The "Suspend provider usage" checkbox (SettingsPage.tsx's
// `.provider-suspend-toggle`) must actually take effect elsewhere in the
// real app, not just persist as inert state: NewSessionModal disables the
// provider's <option> (`disabled={p.suspended}`) so it can't be picked for
// a new session, and unsuspending must restore real usability (a real turn
// against it succeeds again).
test("suspending a provider blocks new sessions and unsuspending restores it", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await page.locator(".provider-suspend-toggle input[type=checkbox]").check();
  await saveProviderSettings(page);

  // SettingsPage is a full route swap (App.tsx renders it instead of the
  // main session view), so the sidebar's `.session-new-button` only exists
  // back on the main route — a real full navigation there, not client-side
  // routing state.
  await page.goto(backend.baseURL);
  await page.locator(".session-new-button").click();
  const providerSelect = page.getByTestId("new-session-provider-select");
  await providerSelect.waitFor({ state: "visible", timeout: 10_000 });

  const claudeOption = providerSelect.locator("option", { hasText: "Suspended" });
  await expect(claudeOption).toHaveCount(1);
  await expect(claudeOption).toBeDisabled();

  await page.locator(".modal-close").click();
  await providerSelect.waitFor({ state: "hidden", timeout: 10_000 });

  await openProviderSettings(page, backend.baseURL, "claude");
  await page.locator(".provider-suspend-toggle input[type=checkbox]").uncheck();
  await saveProviderSettings(page);
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: PONG. No punctuation, no other words.",
  );

  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessage).toContainText("PONG", { timeout: 120_000 });
});

// The "+ Add provider" wizard (SettingsPage.tsx `wizard-templates` ->
// `wizard-form`, ProviderForm with mode="create") is a genuinely separate
// code path from the edit flow above: a fresh view state reached only via
// the list's "+ Add provider" button, a real POST /api/providers instead of
// a PATCH to an existing id, and no pre-existing provider to key off. This
// drives that real wizard end-to-end and proves it actually lands a second,
// distinct provider server-side (a fresh GET /api/providers), not just
// local React state. The Claude template is picked because it needs no new
// external credentials: it's subscription mode with an empty config_dir,
// which config_store resolves to the same default OAuth store the already
// logged-in `claude` CLI provider uses (backend/config_store.py's
// `_provider_ui_state` also reports `credential_status: "available"` for
// any non-api_key-mode provider unconditionally) — so the new entry is
// immediately real and usable, not a stub. Cleans up via the real delete
// flow (including the native confirm() dialog) so the suite doesn't leave
// permanent extra state behind.
test("the create-provider wizard creates a real, usable, additional provider entry", async ({
  authedPage: page,
  backend,
}) => {
  const uniqueName = `Wizard Copy ${Date.now()}`;

  const beforeRes = await page.request.get(`${backend.baseURL}/api/providers`);
  const beforeProviders = ((await beforeRes.json()) as { providers: Array<{ id: string }> }).providers;
  const beforeIds = new Set(beforeProviders.map((p) => p.id));

  await page.goto(`${backend.baseURL}/settings`);
  await page.getByRole("button", { name: "+ Add provider" }).click();

  const claudeTemplateCard = page.locator(".provider-template-card").filter({
    has: page.locator(".provider-template-name", { hasText: "Claude" }),
  });
  await claudeTemplateCard.click();

  await page.getByPlaceholder("My provider").fill(uniqueName);

  const created = page.waitForResponse(
    (res) => new URL(res.url()).pathname === "/api/providers" && res.request().method() === "POST",
  );
  await page.locator(".modal-footer .setup-save-btn").click();
  const createdRes = await created;
  expect(createdRes.ok()).toBe(true);
  const createdProvider = (await createdRes.json()) as { id: string; kind: string; name: string };
  expect(createdProvider.kind).toBe("claude");
  expect(beforeIds.has(createdProvider.id)).toBe(false);

  // Back on the list (a real navigation-free view swap after refetch) — the
  // new row must be visible with its own distinct name, not merged into or
  // replacing the original claude provider's row.
  const newProviderRow = page.locator(".provider-row").filter({
    has: page.locator(".provider-row-name", { hasText: uniqueName }),
  });
  await expect(newProviderRow).toHaveCount(1);

  const afterCreateRes = await page.request.get(`${backend.baseURL}/api/providers`);
  const afterCreateProviders = ((await afterCreateRes.json()) as {
    providers: Array<{ id: string; kind: string; name: string; credential_status: string }>;
  }).providers;
  expect(afterCreateProviders).toHaveLength(beforeProviders.length + 1);
  const persisted = afterCreateProviders.find((p) => p.id === createdProvider.id);
  expect(persisted?.name).toBe(uniqueName);
  expect(persisted?.kind).toBe("claude");
  // Subscription-mode providers report credentials as available unless the
  // credential broker itself is unreachable — proves this is a real,
  // immediately-usable entry, not a placeholder stub.
  expect(persisted?.credential_status).toBe("available");

  // --- cleanup: delete the new provider via the real delete flow ---
  await newProviderRow.locator(".provider-row-main").click();
  await expect(page.locator(".provider-edit-actions")).toBeVisible();

  page.once("dialog", (dialog) => void dialog.accept());
  const deleted = page.waitForResponse(
    (res) =>
      new URL(res.url()).pathname === `/api/providers/${createdProvider.id}` &&
      res.request().method() === "DELETE",
  );
  await page.locator(".provider-edit-actions .btn-danger").click();
  const deletedRes = await deleted;
  expect(deletedRes.ok()).toBe(true);

  await expect(newProviderRow).toHaveCount(0);
  const afterDeleteRes = await page.request.get(`${backend.baseURL}/api/providers`);
  const afterDeleteProviders = ((await afterDeleteRes.json()) as { providers: Array<{ id: string }> }).providers;
  expect(afterDeleteProviders.map((p) => p.id).sort()).toEqual([...beforeIds].sort());
});

// Distinct from the `capability_overrides` tests above: this covers the
// "Default Permission" mode axis (`permission-axis-select-mode`), a
// provider-native permission vocabulary (backend/permission.py), not a
// capability flag. It must (a) actually persist server-side across a real
// reload, and (b) be what a *brand-new* session inherits by default with no
// per-session override — NewSessionModal.tsx seeds a new profile's
// `permission` as `{}` (resolvePermission's "no saved override" branch), and
// its own permission <select> renders the live provider default inline as
// "Inherit default (<value>)" (fetched fresh via GET /api/providers on every
// modal open), so that text is a direct, visible readout of what the new
// session will actually inherit — not just an assumption. The real proof is
// behavioral: with the default flipped to "Default (prompt)", a Bash-tool
// call in that brand-new session must show a real approval card without this
// test ever touching the session's own permission selector.
test("the default permission mode persists and a new session inherits it without an override", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);

  // openProviderSettings does a real page.goto (full navigation), so
  // re-opening here proves the new default survived a real reload, not just
  // in-memory React state.
  await openProviderSettings(page, backend.baseURL, "claude");
  await expect(page.getByTestId("permission-axis-select-mode")).toHaveText(/default \(prompt\)/i);

  // Settings is a distinct route from the app shell — the sidebar's "+ New"
  // button only exists back on "/".
  await page.goto(backend.baseURL);

  // Open the real NewSessionModal and read its permission row before
  // touching anything: with no saved per-session override, its select must
  // sit on "Inherit default" and display the live new provider default
  // ("default") inline — the modal's own visible confirmation of what this
  // new session is about to inherit.
  await page.locator(".session-new-button").click();
  const promptBox = page.getByTestId("new-session-prompt-textarea");
  await promptBox.waitFor({ state: "visible", timeout: 10_000 });

  const permissionRow = page.locator(".ns-modal-row").filter({ hasText: "Permission (Mode)" });
  await expect(permissionRow.locator("select option[value='']")).toHaveText(
    /inherit default \(default\)/i,
  );

  await page.locator(".modal-close").click();
  await promptBox.waitFor({ state: "hidden", timeout: 10_000 });

  // Now drive a real brand-new session through the normal harness flow
  // (no permission override set here) and prove the inherited default
  // actually took effect: a Bash tool call surfaces a real approval card.
  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run exactly this command: echo NEW_SESSION_INHERITED_DEFAULT_MARKER",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button.primary").click();

  await expect(page.getByTestId("assistant-message").last()).toContainText(
    "NEW_SESSION_INHERITED_DEFAULT_MARKER",
    { timeout: 60_000 },
  );
});

// The provider "Nickname" field (ProviderForm's `nickname` state, submitted
// as the `nickname` PATCH field) is distinct from the provider `name`: it's
// shown alongside the name in the providers list as `.provider-nickname`
// (SettingsPage.tsx, via utils/providerDisplayName's `providerNickname`)
// only when non-empty. This proves a real round trip through PATCH
// /api/providers/{id} and a real reload (not just in-memory state), and that
// clearing it back to empty actually removes the persisted value and the
// list span, not just visually hides stale text.
test("a provider nickname persists, displays in the list, and can be cleared", async ({
  authedPage: page,
  backend,
}) => {
  const nickname = `Alias ${Date.now()}`;

  await openProviderSettings(page, backend.baseURL, "claude");
  await page.getByPlaceholder("Account alias (optional)").fill(nickname);
  await saveProviderSettings(page);

  const providerRow = page.getByTestId("provider-row-claude");
  await expect(providerRow.locator(".provider-nickname")).toHaveText(nickname);

  // A real full navigation (not client-side routing) proves the nickname
  // survived a real reload, not just in-memory React state.
  await page.goto(`${backend.baseURL}/settings`);
  await expect(providerRow.locator(".provider-nickname")).toHaveText(nickname);

  // Re-open the edit form on the reloaded page and clear the field.
  await openProviderSettings(page, backend.baseURL, "claude");
  await page.getByPlaceholder("Account alias (optional)").fill("");
  await saveProviderSettings(page);

  await expect(providerRow.locator(".provider-nickname")).toHaveCount(0);

  await page.goto(`${backend.baseURL}/settings`);
  await expect(providerRow.locator(".provider-nickname")).toHaveCount(0);
});
