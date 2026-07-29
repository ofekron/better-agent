import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates the real orchestration + provider + WebSocket wiring: a prompt
// typed into the real UI drives a REAL `claude` CLI subprocess turn, and the
// response streams back over the real /ws/chat WebSocket into the real
// MessageBubble DOM — no mocked backend, no mocked provider.
test("sends a prompt and receives a real assistant response", async ({ authedPage: page }) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: PONG. No punctuation, no other words.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessage).toContainText("PONG", { timeout: 90_000 });
});
