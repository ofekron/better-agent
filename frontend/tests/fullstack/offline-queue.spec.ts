import type { Page } from "@playwright/test";
import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Durable backlog: the action survived to localStorage (not just React
// state), matching the "durable frontend localStorage backlog" contract — a
// reload while still offline would not lose the user's prompt(s).
function readBacklogPrompts(page: Page): Promise<(string | undefined)[]> {
  return page.evaluate(() => {
    const raw = localStorage.getItem("better_agent_offline_queue");
    if (!raw) return [];
    try {
      return (JSON.parse(raw) as Array<{ prompt?: string }>).map((e) => e.prompt);
    } catch {
      return [];
    }
  });
}

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

  expect(await readBacklogPrompts(page)).toContain(offlinePrompt);

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
  await expect.poll(() => readBacklogPrompts(page)).not.toContain(offlinePrompt);
});

// Validates that MULTIPLE prompts queued while offline are flushed in strict
// FIFO order on reconnect, not just that a single queued prompt survives.
// App.tsx's flush loop (the `for (const entry of offlineQueue.getAll())`
// pass that dispatches `create_session`/`send_message` entries once the
// socket reconnects) walks the durable backlog with a plain `for...of` and
// `await`s each dispatch before moving to the next entry — including an
// explicit early `return` on a transient failure specifically to "preserve
// strict action order" (never dispatch a later action ahead of an earlier
// one still waiting on the network). Order is otherwise preserved end to
// end: `enqueue` appends without reordering, `dedupeEntries` keeps
// first-seen position, and nothing in the queue hook sorts or races entries
// against each other.
test("queues two prompts while offline and delivers both in order on reconnect", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const alphaPrompt = "Reply with exactly the single word: ALPHA. No punctuation, no other words.";
  const betaPrompt = "Reply with exactly the single word: BETA. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");

  await textarea.fill(alphaPrompt);
  await textarea.press("Enter");

  // Turn 1 (FIRST) already occupies index 0 of both message lists, so the
  // queued ALPHA turn's user-message box is index 1.
  const alphaMessage = page.getByTestId("user-message").nth(1);
  await expect(alphaMessage).toContainText(alphaPrompt);
  await expect(alphaMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(alphaMessage.locator(".status-offline")).toContainText("Queued offline");

  await textarea.fill(betaPrompt);
  await textarea.press("Enter");

  const betaMessage = page.getByTestId("user-message").nth(2);
  await expect(betaMessage).toContainText(betaPrompt);
  await expect(betaMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(betaMessage.locator(".status-offline")).toContainText("Queued offline");

  // Both queued actions are durable, and in FIFO order — proof this is a
  // real ordered backlog, not just "eventually contains both".
  expect(await readBacklogPrompts(page)).toEqual([alphaPrompt, betaPrompt]);

  await page.context().setOffline(false);

  // Proof the flush actually reached the real backend wire for both queued
  // turns, not merely a UI relabel.
  await expect(alphaMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });
  await expect(betaMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });

  // Proof both actions were genuinely re-delivered through the real
  // backend's normal creation/persistence/provider-turn path, and in the
  // correct order: the assistant reply at index 1 answers ALPHA's turn, the
  // one at index 2 answers BETA's turn — never swapped.
  const alphaReply = page.getByTestId("assistant-message").nth(1);
  const betaReply = page.getByTestId("assistant-message").nth(2);
  await expect(alphaReply).toContainText("ALPHA", { timeout: 120_000 });
  await expect(betaReply).toContainText("BETA", { timeout: 120_000 });

  // The durable backlog is fully drained only on explicit backend
  // acknowledgement of both entries.
  await expect.poll(() => readBacklogPrompts(page)).toEqual([]);
});

// Validates "flapping" connectivity — offline, then rapid online/offline
// flips that could each start (and interrupt) a flush attempt — never loses
// or duplicates a queued prompt. This is the "acceptable simpler variant"
// from the task: precisely timing a single "just long enough to start a
// flush" online window is inherently racy (it depends on exactly how far
// the flush loop got before the next `setOffline(true)` lands), so instead
// this drives several rapid flips back-to-back with no waits or delivery
// assertions in between — each `setOffline` call is awaited (so the browser
// context's network state change is applied) but nothing paces the flips
// with a sleep, and nothing is asserted about message/backlog state until
// the connection is finally left online for good. That keeps the test
// deterministic (no fixed delay to tune, no dependency on winning a race
// against the flush) while still genuinely exercising interrupted-flush
// behavior: if the flush loop is not robust to the socket dropping mid-turn,
// this reliably surfaces it as either a lost prompt (never delivered) or a
// duplicate delivery (two assistant replies), not as a flaky timeout.
test("flapping connectivity does not lose or duplicate a queued prompt", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const flapPrompt = "Reply with exactly the single word: THIRD. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(flapPrompt);
  await textarea.press("Enter");

  const flapMessage = page.getByTestId("user-message").last();
  await expect(flapMessage).toContainText(flapPrompt);
  await expect(flapMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });

  expect(await readBacklogPrompts(page)).toContain(flapPrompt);

  // Rapid flips: each may or may not manage to kick off a flush attempt
  // before the next flip cuts it off again — deliberately not asserting
  // anything about delivery here, only that the browser's own connectivity
  // signal tracks each flip, so no flip is silently a no-op.
  for (let i = 0; i < 3; i++) {
    await page.context().setOffline(false);
    await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(true);
    await page.context().setOffline(true);
    await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);
  }

  // Now go online for real and stay online.
  await page.context().setOffline(false);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(true);

  // Proof the flush actually reached the real backend wire, not merely a UI
  // relabel.
  await expect(flapMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });

  // Delivered exactly once: not zero (lost by an interrupted flush), not two
  // (duplicated by a flush that re-dispatched an entry it had already sent
  // before an earlier flip cut it off).
  await expect(page.getByTestId("assistant-message").last()).toContainText("THIRD", {
    timeout: 120_000,
  });
  await expect
    .poll(async () => (await page.getByTestId("assistant-message").allTextContents()).filter((t) => t.includes("THIRD")).length)
    .toBe(1);

  // The durable backlog converges to empty — no leftover or resurrected
  // entry from an interrupted flush attempt.
  await expect.poll(() => readBacklogPrompts(page)).toEqual([]);
});
