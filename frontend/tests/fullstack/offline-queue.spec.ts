import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates the real offline-action backlog (AGENTS.md "Offline-first
// usability"): a prompt sent while the page cannot reach the real backend is
// (1) durably queued in localStorage, (2) shown with a truthful "offline"
// pending indicator, and (3) actually delivered through the real backend's
// normal creation/persistence/provider-turn path once the network returns —
// not just flipped to a fake "success" in the UI.
//
// `page.context().setOffline(true)` blocks the browser's real network
// traffic (HTTP + the open WebSocket) to the real backend subprocess, which
// keeps running the whole time — this is a real network blip, not a mock.
test("queues a prompt while offline and delivers it through the real backend on reconnect", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const offlinePrompt = "Reply with exactly the single word: SECOND. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(offlinePrompt);
  await textarea.press("Enter");

  // The second turn's user-message box is the last one on the page (no
  // third turn exists yet) — this locator re-queries on every assertion, so
  // it keeps tracking the same turn's box across the later status flip.
  const queuedMessage = page.getByTestId("user-message").last();
  await expect(queuedMessage).toContainText(offlinePrompt);
  await expect(queuedMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(queuedMessage.locator(".status-offline")).toContainText("Queued offline");

  // Durable backlog: the action survived to localStorage (not just React
  // state), matching the "durable frontend localStorage backlog" contract —
  // a reload while still offline would not lose the user's prompt.
  const readBacklogPrompts = () =>
    page.evaluate(() => {
      const raw = localStorage.getItem("better_agent_offline_queue");
      if (!raw) return [];
      try {
        return (JSON.parse(raw) as Array<{ prompt?: string }>).map((e) => e.prompt);
      } catch {
        return [];
      }
    });
  expect(await readBacklogPrompts()).toContain(offlinePrompt);

  await page.context().setOffline(false);

  // Proof the flush actually reached the real backend wire, not merely a UI
  // relabel: the pending bubble stops claiming "offline" once acknowledged.
  await expect(queuedMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });

  // Proof the action was genuinely re-delivered through the real backend's
  // normal creation/persistence/provider-turn path: a real assistant turn
  // answers the prompt that was queued while offline.
  await expect(page.getByTestId("assistant-message").last()).toContainText("SECOND", {
    timeout: 120_000,
  });

  // The durable backlog entry is cleared only on explicit backend
  // acknowledgement.
  await expect.poll(readBacklogPrompts).not.toContain(offlinePrompt);
});
