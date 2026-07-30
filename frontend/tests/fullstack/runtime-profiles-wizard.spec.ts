import type { Page } from "@playwright/test";
import { test, expect } from "./harness/fixtures";
import type { FullStackBackend } from "./harness/backend";
import { createSessionWithoutTurn } from "./harness/session";

// Runtime-profile lifecycle through the REAL UI + backend: sessions are
// created via a runtime-profile picker (provider/runner pickers are gone),
// profiles are managed in Settings > Runtime Profiles (3-step wizard:
// provider -> runner -> defaults; Activate is the ONLY default-selection
// surface; delete is a soft-delete that leaves a visible tombstone), all
// backed by /api/runtime-profiles + the runtime_profiles_changed WS frame.
//
// IMPORTANT: none of these tests sends a prompt, so NO real provider model
// turn is ever spent — session creation uses the modal's split-button
// "Create" action (create only, no send). Keep it that way: turn-spending
// coverage lives in the other specs and stays behind the suite's normal
// LLM-run approval gating.

interface RuntimeProfileRecord {
  id: string;
  provider_id: string;
  runner: string;
  name: string;
  deleted_at: string | null;
}

interface RuntimeProfilesSnapshot {
  runtime_profiles: RuntimeProfileRecord[];
  default_runtime_profile_id: string | null;
}

interface ProviderRecord {
  id: string;
  kind: string;
  runner_options: string[];
}

async function fetchSnapshot(page: Page, backend: FullStackBackend): Promise<RuntimeProfilesSnapshot> {
  const res = await page.request.get(`${backend.baseURL}/api/runtime-profiles`);
  expect(res.ok()).toBe(true);
  return (await res.json()) as RuntimeProfilesSnapshot;
}

async function fetchClaudeProvider(page: Page, backend: FullStackBackend): Promise<ProviderRecord> {
  const res = await page.request.get(`${backend.baseURL}/api/providers`);
  expect(res.ok()).toBe(true);
  const providers = ((await res.json()) as { providers: ProviderRecord[] }).providers;
  const claude = providers.find((p) => p.kind === "claude");
  expect(claude).toBeTruthy();
  return claude!;
}

/** A (provider, runner) pair with no live profile yet — the fresh install
 * seeds exactly one profile on the provider's default runner, and the
 * subscription-mode claude provider exposes a second runner choice. */
async function findFreeRunner(page: Page, backend: FullStackBackend): Promise<{ provider: ProviderRecord; runner: string }> {
  const provider = await fetchClaudeProvider(page, backend);
  const snapshot = await fetchSnapshot(page, backend);
  const taken = new Set(
    snapshot.runtime_profiles
      .filter((p) => p.provider_id === provider.id && !p.deleted_at)
      .map((p) => p.runner),
  );
  const runner = provider.runner_options.find((r) => !taken.has(r));
  expect(runner, `no free runner on ${provider.runner_options.join(",")}`).toBeTruthy();
  return { provider, runner: runner! };
}

/** Creates a second live profile directly via the REST API (the same
 * endpoint the wizard posts to) for tests whose subject is what happens
 * AFTER a profile exists — the wizard UI itself is covered end-to-end in
 * the first test. */
async function createProfileViaApi(
  page: Page,
  backend: FullStackBackend,
  name: string,
): Promise<RuntimeProfileRecord> {
  const { provider, runner } = await findFreeRunner(page, backend);
  const res = await page.request.post(`${backend.baseURL}/api/runtime-profiles`, {
    data: { provider_id: provider.id, runner, name },
  });
  expect(res.ok()).toBe(true);
  return (await res.json()) as RuntimeProfileRecord;
}

function openRuntimeProfilesSettings(page: Page, backend: FullStackBackend): Promise<unknown> {
  return page.goto(`${backend.baseURL}/settings?section=runtimeProfiles`);
}

// Drives the real 3-step wizard end-to-end for the EXISTING claude provider:
// Settings > Runtime Profiles > "Create runtime profile" -> pick the provider
// card -> Continue -> runner cards (the seeded profile's runner must show the
// duplicate-pair guard: disabled + "A live profile already exists for this
// runner.") -> pick the free runner -> defaults step -> name it -> a real
// POST /api/runtime-profiles lands the profile, and the management rail
// shows it — server-persisted, not just local React state.
test("the wizard creates a runtime profile for an existing provider", async ({
  authedPage: page,
  backend,
}) => {
  const { provider, runner: freeRunner } = await findFreeRunner(page, backend);
  const snapshotBefore = await fetchSnapshot(page, backend);
  const seededDefault = snapshotBefore.runtime_profiles.find(
    (p) => p.id === snapshotBefore.default_runtime_profile_id,
  );
  expect(seededDefault).toBeTruthy();

  await openRuntimeProfilesSettings(page, backend);
  await page.getByTestId("rpe-create-profile").click();

  // Step 1: provider pick — the existing claude provider's card.
  await expect(page.getByRole("heading", { name: "New runtime profile" })).toBeVisible();
  await page.locator(".rpw-provider-card").first().click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();

  // Step 2: runner cards. The seeded profile already occupies its runner, so
  // that card is the duplicate-pair guard: disabled with the taken-hint.
  const takenCard = page.getByTestId(`rpw-runner-${seededDefault!.runner}`);
  await expect(takenCard).toBeDisabled();
  await expect(takenCard).toContainText("A live profile already exists for this runner.");

  // Picking an enabled runner card advances straight to the defaults step.
  const freeCard = page.getByTestId(`rpw-runner-${freeRunner}`);
  await expect(freeCard).toBeEnabled();
  await freeCard.click();

  // Step 3: defaults — name the profile and submit through the real POST.
  const profileName = `Wizard Profile ${Date.now()}`;
  const nameField = page
    .locator(".rpw-step .setup-field")
    .filter({ hasText: "Profile name" })
    .locator("input");
  await nameField.fill(profileName);

  const created = page.waitForResponse(
    (res) =>
      new URL(res.url()).pathname === "/api/runtime-profiles" &&
      res.request().method() === "POST",
  );
  await page.getByTestId("rpw-create-profile").click();
  const createdRes = await created;
  expect(createdRes.ok()).toBe(true);
  const createdProfile = (await createdRes.json()) as RuntimeProfileRecord;
  expect(createdProfile.provider_id).toBe(provider.id);
  expect(createdProfile.runner).toBe(freeRunner);
  expect(createdProfile.name).toBe(profileName);

  // The wizard returns to the management rail, which must list the new
  // profile alongside the seeded one.
  await expect(page.getByTestId(`rpe-rail-${createdProfile.id}`)).toBeVisible();
  await expect(page.getByTestId(`rpe-rail-${createdProfile.id}`)).toContainText(profileName);

  // Server truth: the profile is live in the snapshot, and creating it did
  // NOT move the default (Activate is the only default-selection surface).
  const snapshotAfter = await fetchSnapshot(page, backend);
  const persisted = snapshotAfter.runtime_profiles.find((p) => p.id === createdProfile.id);
  expect(persisted?.deleted_at ?? null).toBeNull();
  expect(snapshotAfter.default_runtime_profile_id).toBe(snapshotBefore.default_runtime_profile_id);
});

// One live profile per (provider, runner) pair: re-posting the seeded
// profile's exact pair must be rejected with the duplicate-pair error and
// must not grow the profile list — proving the uniqueness constraint is
// enforced server-side, not just by the wizard's disabled runner card
// (covered in the wizard test above).
test("a duplicate (provider, runner) pair is rejected server-side", async ({
  authedPage: page,
  backend,
}) => {
  const provider = await fetchClaudeProvider(page, backend);
  const snapshotBefore = await fetchSnapshot(page, backend);
  const seededDefault = snapshotBefore.runtime_profiles.find(
    (p) => p.id === snapshotBefore.default_runtime_profile_id,
  );
  expect(seededDefault).toBeTruthy();

  const res = await page.request.post(`${backend.baseURL}/api/runtime-profiles`, {
    data: {
      provider_id: provider.id,
      runner: seededDefault!.runner,
      name: `Duplicate ${Date.now()}`,
    },
  });
  expect(res.status()).toBe(400);
  const body = (await res.json()) as { detail?: string };
  expect(body.detail).toMatch(/already exists/i);

  const snapshotAfter = await fetchSnapshot(page, backend);
  expect(snapshotAfter.runtime_profiles).toHaveLength(snapshotBefore.runtime_profiles.length);
});

// Activate is the single default-selection surface (the per-provider
// set-default endpoint is gone). Activating a profile through the real
// button must move the Default badge in the rail, persist across a real
// full reload, and be reflected in the snapshot's
// default_runtime_profile_id.
test("activating a profile moves the default and persists across a reload", async ({
  authedPage: page,
  backend,
}) => {
  const snapshotBefore = await fetchSnapshot(page, backend);
  const originalDefaultId = snapshotBefore.default_runtime_profile_id;
  expect(originalDefaultId).toBeTruthy();

  const profile = await createProfileViaApi(page, backend, `Activate Me ${Date.now()}`);

  await openRuntimeProfilesSettings(page, backend);
  await page.getByTestId(`rpe-rail-${profile.id}`).click();

  const activated = page.waitForResponse(
    (res) =>
      new URL(res.url()).pathname === `/api/runtime-profiles/${profile.id}/activate` &&
      res.request().method() === "POST",
  );
  await page.getByTestId("rpe-activate").click();
  expect((await activated).ok()).toBe(true);

  // The Default badge moves: onto the activated profile's rail item, off
  // the previous default's.
  await expect(
    page.getByTestId(`rpe-rail-${profile.id}`).locator(".provider-active-pill"),
  ).toBeVisible();
  await expect(
    page.getByTestId(`rpe-rail-${originalDefaultId}`).locator(".provider-active-pill"),
  ).toHaveCount(0);

  // A real full navigation (not client-side state) proves persistence.
  await openRuntimeProfilesSettings(page, backend);
  await expect(
    page.getByTestId(`rpe-rail-${profile.id}`).locator(".provider-active-pill"),
  ).toBeVisible();

  const snapshotAfter = await fetchSnapshot(page, backend);
  expect(snapshotAfter.default_runtime_profile_id).toBe(profile.id);
});

// Soft-delete with tombstones: deleting a non-default profile keeps it in
// the rail as a visible "Deleted" tombstone (old sessions keep resolving
// its display name) while removing it from the new-session profile picker.
// The default profile is protected from deletion in both the UI (no delete
// button, an explanatory hint instead) and the API (409) — activate another
// profile first.
test("soft-deleting a profile leaves a visible tombstone and hides it from the picker", async ({
  authedPage: page,
  backend,
}) => {
  const snapshot = await fetchSnapshot(page, backend);
  const defaultId = snapshot.default_runtime_profile_id;
  expect(defaultId).toBeTruthy();

  const profileName = `Doomed Profile ${Date.now()}`;
  const profile = await createProfileViaApi(page, backend, profileName);

  // The default profile refuses deletion server-side.
  const deleteDefaultRes = await page.request.delete(
    `${backend.baseURL}/api/runtime-profiles/${defaultId}`,
  );
  expect(deleteDefaultRes.status()).toBe(409);

  await openRuntimeProfilesSettings(page, backend);

  // ...and in the UI: selecting the default shows the cannot-delete hint
  // instead of a delete button.
  await page.getByTestId(`rpe-rail-${defaultId}`).click();
  await expect(page.getByTestId("rpe-delete")).toHaveCount(0);
  await expect(page.locator(".provider-edit-actions .setup-field-hint")).toContainText(
    "The default profile cannot be deleted",
  );

  // Soft-delete the non-default profile through the real confirm() gate.
  await page.getByTestId(`rpe-rail-${profile.id}`).click();
  page.once("dialog", (dialog) => void dialog.accept());
  const deleted = page.waitForResponse(
    (res) =>
      new URL(res.url()).pathname === `/api/runtime-profiles/${profile.id}` &&
      res.request().method() === "DELETE",
  );
  await page.getByTestId("rpe-delete").click();
  expect((await deleted).ok()).toBe(true);

  // The tombstone stays visible in the rail with the Deleted badge, and the
  // pane offers Restore (the provider is still alive).
  const tombstoneRail = page.getByTestId(`rpe-rail-${profile.id}`);
  await expect(tombstoneRail).toBeVisible();
  await expect(tombstoneRail.locator(".rpe-deleted-badge")).toHaveText("Deleted");
  await tombstoneRail.click();
  await expect(page.getByTestId("rpe-restore")).toBeVisible();

  // Server truth: tombstoned (deleted_at set), not hard-deleted.
  const snapshotAfter = await fetchSnapshot(page, backend);
  const tombstone = snapshotAfter.runtime_profiles.find((p) => p.id === profile.id);
  expect(tombstone?.deleted_at).toBeTruthy();

  // The new-session profile picker only offers LIVE profiles — the
  // tombstone must not be pickable.
  await page.goto(backend.baseURL);
  await page.locator(".session-new-button").click();
  const profileSelect = page.getByTestId("new-session-profile-select");
  await profileSelect.waitFor({ state: "visible", timeout: 10_000 });
  await expect(profileSelect.locator("option", { hasText: profileName })).toHaveCount(0);
});

// Session creation goes through the runtime-profile picker: the old
// provider/runner selects are gone from NewSessionModal, picking a profile
// is the session-identity choice, and the created session record is stamped
// with that profile's id. Uses the split-button "Create" action — the
// session is real and server-persisted, but NO prompt is sent and NO
// provider model turn is spent.
test("a session created via a new profile is stamped with that profile's id", async ({
  authedPage: page,
  backend,
}) => {
  const profileName = `Session Backer ${Date.now()}`;
  const profile = await createProfileViaApi(page, backend, profileName);

  // Structural: the profile picker replaced the provider/runner pickers.
  await page.locator(".session-new-button").click();
  await page.getByTestId("new-session-prompt-textarea").waitFor({ state: "visible", timeout: 10_000 });
  await expect(page.getByTestId("new-session-profile-select")).toBeVisible();
  await expect(page.getByTestId("new-session-provider-select")).toHaveCount(0);
  await expect(page.getByTestId("new-session-runner-select")).toHaveCount(0);
  await page.locator(".modal-close").click();
  await page.getByTestId("new-session-prompt-textarea").waitFor({ state: "hidden", timeout: 10_000 });

  const session = await createSessionWithoutTurn(page, {
    profilePattern: new RegExp(profileName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
  });
  expect(session.id).toBeTruthy();
  expect(session.runtime_profile_id).toBe(profile.id);

  // Server truth on a fresh read, not just the POST echo.
  const detailRes = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(session.id)}?msg_limit=5`,
  );
  expect(detailRes.ok()).toBe(true);
  const detail = (await detailRes.json()) as { runtime_profile_id?: string | null };
  expect(detail.runtime_profile_id).toBe(profile.id);
});
