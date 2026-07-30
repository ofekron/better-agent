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
