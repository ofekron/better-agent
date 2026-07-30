import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";
import { openProviderSettings, pickCustomSelectOption, saveProviderSettings } from "./harness/settings";

// Fresh providers default to full permission bypass (no approval prompts),
// so this switches the real claude provider's permission "mode" axis to
// "default" (prompt) via the real Settings UI first, then drives a real
// turn that needs a Bash tool call and exercises the real approve/deny UI.
test("prompts for and honors a real tool approval", async ({ authedPage: page, backend }) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  // Settings is a distinct route from the app shell — createSessionWithPrompt
  // needs the sidebar's "+ New" button, which only exists on "/".
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run exactly this command: echo APPROVED_MARKER",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button.primary").click();

  await expect(page.getByTestId("assistant-message").last()).toContainText("APPROVED_MARKER", {
    timeout: 60_000,
  });
});

test("a denied tool approval blocks the tool call", async ({ authedPage: page, backend }) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  // Settings is a distinct route from the app shell — createSessionWithPrompt
  // needs the sidebar's "+ New" button, which only exists on "/".
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run exactly this command: echo SHOULD_NOT_APPEAR",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button:not(.primary)").click();

  await expect(page.getByTestId("chat-messages")).not.toContainText("SHOULD_NOT_APPEAR", {
    timeout: 15_000,
  });
});
