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
  await expect(assistantMessage).toContainText("PONG", { timeout: 120_000 });
});

// Validates the real interrupt path: a long-running turn against the real
// `claude` CLI subprocess can be stopped mid-stream via the real InputArea
// stop control, and the UI reflects the interruption rather than silently
// continuing or leaving a permanently blank bubble.
test("interrupts a real in-flight turn", async ({ authedPage: page }) => {
  await createSessionWithPrompt(
    page,
    "Count from 1 to 100 slowly, one number per line, explaining each number's factors.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  // InputArea only renders `stop-btn` while `somethingRunning` (isStreaming)
  // is true, so its visibility is a direct proxy for "the turn is actually
  // running" — wait for that rather than an arbitrary delay.
  const stopBtn = page.getByTestId("stop-btn");
  await expect(stopBtn).toBeVisible({ timeout: 30_000 });

  await stopBtn.click();

  // The same element unmounts the instant `isStreaming` flips false, so
  // waiting for it to disappear is a direct, event-driven signal that
  // streaming actually stopped (not just that the click was sent).
  await expect(stopBtn).toBeHidden({ timeout: 30_000 });

  // The assistant bubble must show a terminal "stopped" signal — the
  // StoppedIndicator rendered from `message.stopped_at` — instead of
  // vanishing or silently continuing to look like it's still running.
  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible();
  await expect(assistantMessage.locator(".stopped-indicator")).toBeVisible({ timeout: 15_000 });
});

// Validates the real queuing path: a second prompt typed and submitted
// while the first turn is still streaming does NOT interrupt it. Instead
// it is held as a queued prompt (visible in the real InputArea banner)
// and the real backend session queue automatically dequeues + runs it as
// its own turn once the first one finishes — no further user action.
//
// The default session provider (native `claude` CLI) does not set
// `supports_steering`, so `canSteer` is false and `steerIsPrimary` is
// false: InputArea renders a single primary `send-btn` (labelled
// "queueSendButton" while streaming) rather than a separate steer/queue
// pair. That single button is what submits the second prompt here.
test("queues a second prompt while a turn is running, then runs it after", async ({ authedPage: page }) => {
  await createSessionWithPrompt(
    page,
    "Count from 1 to 50 slowly, one number per line, explaining each number's factors.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  // Same event-driven proxy for "the turn is actually running" as the
  // interrupt test above.
  const stopBtn = page.getByTestId("stop-btn");
  await expect(stopBtn).toBeVisible({ timeout: 30_000 });

  const secondPrompt = "Reply with exactly the single word: SECONDTURN. No punctuation, no other words.";
  await page.getByTestId("input-textarea").fill(secondPrompt);
  await page.getByTestId("send-btn").click();

  // The real queued-prompt banner (InputArea.tsx) proves the second
  // prompt was accepted as queued rather than sent as a steer/interrupt.
  const queuedBanner = page.getByTestId("queued-prompt-banner");
  await expect(queuedBanner).toBeVisible({ timeout: 15_000 });
  await expect(queuedBanner).toContainText("SECONDTURN");

  // First turn's assistant reply must complete with real content.
  const assistantMessages = page.getByTestId("assistant-message");
  await expect(assistantMessages.first()).toBeVisible({ timeout: 30_000 });
  await expect(stopBtn).toBeHidden({ timeout: 180_000 });
  await expect(assistantMessages.first()).not.toHaveText("", { timeout: 15_000 });

  // The backend's per-session queue auto-dequeues the held prompt into a
  // real second turn — the banner disappears and a second user/assistant
  // pair appears, with no further UI action from the test.
  await expect(queuedBanner).toBeHidden({ timeout: 30_000 });
  await expect(page.getByTestId("user-message")).toHaveCount(2, { timeout: 30_000 });
  await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
  await expect(assistantMessages.nth(1)).toContainText("SECONDTURN", { timeout: 120_000 });
});
