import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates real fork/delegation: forking from an existing turn spawns a
// real child session, runs a real provider turn in it, and the parent's
// ForkSplitView renders both panes.
test("forks from an existing turn into a real child session", async ({ authedPage: page }) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const forkPane = page.getByTestId("fork-pane");
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });
});
