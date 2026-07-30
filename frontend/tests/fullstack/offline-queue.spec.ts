import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Page } from "@playwright/test";
import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// A minimal valid 1x1 red PNG — real pixel data (not an empty/fake file), same
// fixture used by chat-turn.spec.ts's online multimodal test.
const ONE_PIXEL_RED_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

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

// Reads the raw queued entry (not just its prompt text) so the image payload
// itself can be inspected for survival/corruption through the JSON.stringify
// round-trip into localStorage.
function readBacklogEntry(
  page: Page,
  prompt: string,
): Promise<{ images?: Array<{ data?: string; media_type?: string }> } | undefined> {
  return page.evaluate((p) => {
    const raw = localStorage.getItem("better_agent_offline_queue");
    if (!raw) return undefined;
    try {
      const entries = JSON.parse(raw) as Array<{ prompt?: string; images?: Array<{ data?: string; media_type?: string }> }>;
      return entries.find((e) => e.prompt === p);
    } catch {
      return undefined;
    }
  }, prompt);
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

// Validates the PERMANENT-rejection path, not just a transient one: once the
// backend genuinely refuses a queued action on its merits, the UI must show
// a real failure state and the durable backlog must stop carrying it — never
// retry it forever and never make it vanish with no trace.
//
// Real rejection path found by reading the source (not guessed): App.tsx's
// flush loop (~App.tsx:2379-2411) dispatches a queued `send_message` over
// the WebSocket for its target session with no existence check of its own.
// The backend's WS handler (backend/main.py, `msg_type == "send_message"`)
// calls `_start_prompt_handoff` -> `admit_queued_prompt_durable`, which
// resolves the session's root id (`session_manager._root_id_for`) before
// admitting the prompt. Deleting the session via the real
// `DELETE /api/sessions/{id}` route removes it from the root index, so a
// prompt admitted afterward finds `admission.get("session")` falsy
// (backend/main.py ~17527) and the backend responds with BOTH a
// `user_message_failed` lifecycle event (reason "session_not_found") and a
// WS `{"type":"error"}` frame (`t("error.session_not_found_retry")`) —
// never silently drops it and never leaves it queued. Frontend-side,
// `handlePromptSendError` (App.tsx ~1648) is what turns that into a visible
// `status: "error"` bubble AND calls `removeAckedOfflineAction`, which is
// the same durable-backlog removal function every other test in this file
// uses to prove real backend acknowledgement — so this is a genuine
// permanent-rejection surfacing, not a UI-only relabel.
//
// `page.request` is Playwright's Node-side APIRequestContext: it shares the
// browser context's auth cookie (so the real DELETE endpoint accepts it) but
// does not go through the browser's emulated network stack, so it is
// unaffected by `page.context().setOffline(true)` — exactly what's needed to
// delete the session out from under the client while the page still
// believes it is offline, mirroring a real "session was cleaned up
// elsewhere while this client was disconnected" scenario.
test("a queued prompt whose session was permanently deleted before reconnect surfaces a real failure, not an infinite retry or silent drop", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  const sessionMatch = page.url().match(/\/s\/([^/?#]+)/);
  if (!sessionMatch) throw new Error(`could not extract session id from URL: ${page.url()}`);
  const sessionId = decodeURIComponent(sessionMatch[1]);

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const rejectedPrompt = "Reply with exactly the single word: REJECTED. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(rejectedPrompt);
  await textarea.press("Enter");

  const rejectedMessage = page.getByTestId("user-message").last();
  await expect(rejectedMessage).toContainText(rejectedPrompt);
  await expect(rejectedMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  expect(await readBacklogPrompts(page)).toContain(rejectedPrompt);

  // Permanently reject the queued action's target: delete the session for
  // real via the backend's own delete route, while the browser page is still
  // offline (page.request bypasses the emulated offline network).
  const deleteRes = await page.request.delete(`${backend.baseURL}/api/sessions/${sessionId}`);
  expect(deleteRes.ok()).toBe(true);

  await page.context().setOffline(false);

  // Real failure surfaces on the bubble — not stuck on "offline" (which
  // would mean it's still silently queued) and not simply gone.
  await expect(rejectedMessage).toHaveAttribute("data-status", "error", { timeout: 30_000 });
  await expect(rejectedMessage.locator(".status-error")).toContainText("Failed");

  // The durable backlog entry is dropped on the backend's explicit permanent
  // rejection — proof this is a terminal failure state, not a retry loop
  // that keeps re-dispatching the same doomed action forever.
  await expect.poll(() => readBacklogPrompts(page)).not.toContain(rejectedPrompt);

  // No assistant reply for REJECTED — the backend never ran a turn for it
  // (admission failed before the orchestrator/provider were ever reached).
  const replies = await page.getByTestId("assistant-message").allTextContents();
  expect(replies.some((t) => t.includes("REJECTED"))).toBe(false);
});

// Validates that an image attachment queued while offline is NOT dropped or
// corrupted by the localStorage round-trip, and is genuinely delivered on
// reconnect.
//
// Read from source before writing this test (useOfflineQueue.ts,
// utils/imageAttach.ts): attachments are NOT raw File/Blob objects by the
// time they reach the queue, so there is no JSON-serialization hazard to
// begin with. `fileToPastedImage` (imageAttach.ts) runs entirely client-side
// — off the browser's `Image`/`canvas` APIs and `FileReader`, no network
// call — the instant a file is selected, turning it into a `PastedImage`
// `{ dataUrl, base64, mediaType }` with `base64` as a plain string. App.tsx's
// `toImagePayload` then maps that straight into `ImagePayload { data:
// string, media_type: string }`, which is what `offlineQueue.enqueue` stores.
// So the attachment is already a plain JSON-serializable string well before
// it ever touches `JSON.stringify`/localStorage — the app doesn't need an
// "attachments disabled while offline" guard because there's nothing
// File/Blob-shaped left to fail to serialize. This test proves that holds in
// practice: the queued entry's `images[0].data` survives the localStorage
// round-trip intact, and the image is actually forwarded to the real
// provider on flush (not silently dropped while the text half of the prompt
// gets through).
test("queues a prompt with an image attachment while offline and delivers the image through the real backend on reconnect", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const imageDir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-offline-image-"));
  const imagePath = path.join(imageDir, "red-pixel.png");
  writeFileSync(imagePath, Buffer.from(ONE_PIXEL_RED_PNG_BASE64, "base64"));

  // Attaching happens entirely client-side (canvas/FileReader), so it works
  // the same whether the browser is online or offline — proof of that is
  // this succeeding at all while `setOffline(true)` is in effect.
  await page.locator('input[type="file"]').setInputFiles(imagePath);
  await expect(page.locator(".image-preview-item")).toHaveCount(1, { timeout: 15_000 });

  const offlinePrompt = "What color is this image? Reply with exactly one word.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(offlinePrompt);
  await textarea.press("Enter");

  const queuedMessage = page.getByTestId("user-message").last();
  await expect(queuedMessage).toContainText(offlinePrompt);
  await expect(queuedMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(queuedMessage.locator(".status-offline")).toContainText("Queued offline");

  // The optimistic pending bubble renders the attachment immediately from
  // local composer state (not from the queue), proving the composer handed
  // the image off to the pending message rather than silently dropping it
  // client-side before it ever reached `enqueue`.
  await expect(queuedMessage.locator(".message-image")).toHaveCount(1);

  // The image left the composer as part of the real send, same as the
  // online multimodal path.
  await expect(page.locator(".image-preview-item")).toHaveCount(0);

  // The real proof this test exists for: the durable localStorage backlog
  // entry carries the image payload as a plain base64 string, not a dropped
  // field, an empty array, or a corrupted `"[object Object]"`/`"[object
  // File]"` stringification artifact.
  const backlogEntry = await readBacklogEntry(page, offlinePrompt);
  expect(backlogEntry?.images?.length).toBe(1);
  const queuedImage = backlogEntry!.images![0];
  expect(queuedImage.media_type).toBe("image/jpeg");
  expect(typeof queuedImage.data).toBe("string");
  expect(queuedImage.data!.length).toBeGreaterThan(100);
  expect(queuedImage.data).toMatch(/^[A-Za-z0-9+/]+={0,2}$/);

  await page.context().setOffline(false);

  // Proof the flush actually reached the real backend wire, not merely a UI
  // relabel.
  await expect(queuedMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });

  // Proof the image specifically (not just the text half of the prompt) was
  // genuinely transmitted to the real provider through the flush: a
  // non-empty assistant reply to a vision question that only makes sense if
  // the model actually received image data. Exact color correctness isn't
  // asserted since that depends on model vision accuracy, not on whether the
  // image round-tripped through the offline queue.
  await expect(page.getByTestId("assistant-message").last()).not.toHaveText("", {
    timeout: 120_000,
  });

  // The durable backlog entry is cleared only on explicit backend
  // acknowledgement.
  await expect.poll(() => readBacklogPrompts(page)).not.toContain(offlinePrompt);
});

// Validates that a queued-while-offline prompt survives a REAL page reload
// that happens while the browser is STILL offline — not just persistence
// across time within one already-mounted page instance (every other test in
// this file only exercises that weaker case). `page.context().setOffline`
// is a browser-context-level network condition (CDP
// `Network.emulateNetworkConditions`), so it stays in effect across
// `page.reload()`'s navigation. On a fresh mount, the app has no in-memory
// React state at all — the pending bubble can only reappear by the offline
// queue hook re-reading `better_agent_offline_queue` from localStorage on
// init, so this proves genuine re-hydration, not state merely surviving
// because the page never actually unloaded.
test("a prompt queued while offline survives a real page reload that still happens while offline", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: FIRST. No punctuation, no other words.",
  );
  await expect(page.getByTestId("assistant-message")).toContainText("FIRST", { timeout: 120_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  const reloadPrompt = "Reply with exactly the single word: RELOADED. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(reloadPrompt);
  await textarea.press("Enter");

  const queuedMessage = page.getByTestId("user-message").last();
  await expect(queuedMessage).toContainText(reloadPrompt);
  await expect(queuedMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(queuedMessage.locator(".status-offline")).toContainText("Queued offline");
  expect(await readBacklogPrompts(page)).toContain(reloadPrompt);

  // Real reload, still offline: the browser context's network condition
  // persists across the navigation, so this is a genuine fresh page load
  // with no network reachable — not a soft client-side re-render.
  await page.reload();
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  // The durable backlog itself survived the fresh page load untouched.
  expect(await readBacklogPrompts(page)).toContain(reloadPrompt);

  // The UI re-hydrates the queued prompt's bubble from that backlog on
  // mount, still correctly marked offline — proof this is real
  // re-hydration, not leftover in-memory state.
  const reloadedMessage = page.getByTestId("user-message").last();
  await expect(reloadedMessage).toContainText(reloadPrompt);
  await expect(reloadedMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(reloadedMessage.locator(".status-offline")).toContainText("Queued offline");

  await page.context().setOffline(false);

  // Proof the flush actually reached the real backend wire after the
  // reload, not merely a UI relabel.
  await expect(reloadedMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });
  await expect(page.getByTestId("assistant-message").last()).toContainText("RELOADED", {
    timeout: 120_000,
  });

  // The durable backlog entry is cleared only on explicit backend
  // acknowledgement.
  await expect.poll(() => readBacklogPrompts(page)).not.toContain(reloadPrompt);
});

// Validates going offline WHILE a turn is actively streaming (not idle) —
// every other test in this file goes offline only after the prior turn has
// already finished. This proves two things that going-offline-while-idle
// cannot: (1) the offline transition never touches the FIRST turn, which is
// a real `claude` CLI subprocess already running server-side and delivered
// back over `messages_replay` on WS reconnect (App.tsx ~4636-4638), so it
// completes exactly as if the browser had stayed online; and (2) a SECOND
// prompt typed while offline goes through the localStorage offline-queue
// path (`data-status="offline"`), not the backend's online per-session
// "queued banner" path (`queued-prompt-banner`) — because App.tsx's
// `sendMessage(...)` call (~App.tsx:4889) simply fails when the WS isn't
// connected regardless of whether a turn is streaming, and that failure is
// what routes the prompt into the offline backlog (~App.tsx:4907-4925). The
// `queued-prompt-banner` only ever appears once the backend WS-acks a queued
// prompt (`queuedBySession`, App.tsx ~2543), which can't happen here since
// the browser never reached the backend at all.
test("goes offline while a turn is actively running, queues the next prompt through the offline path, and both turns complete in order on reconnect", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(
    page,
    "Count from 1 to 40 slowly, one number per line, explaining each number's factors, then on a final line reply with exactly the single word: FIRSTDONE.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  // `stop-btn` only renders while `isStreaming` is true — the same
  // event-driven proxy chat-turn.spec.ts uses for "the turn is actually
  // running". This is the precondition the whole test hinges on: we must
  // go offline while this is still true, not after it disappears.
  const stopBtn = page.getByTestId("stop-btn");
  await expect(stopBtn).toBeVisible({ timeout: 30_000 });

  await page.context().setOffline(true);
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);

  // The offline transition is a pure browser-network event — it must not
  // touch the still-running server-side turn.
  await expect(stopBtn).toBeVisible();

  const secondPrompt = "Reply with exactly the single word: SECONDDONE. No punctuation, no other words.";
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(secondPrompt);
  await textarea.press("Enter");

  // Second bubble (index 1) — the first turn's user/assistant pair still
  // occupies index 0.
  const secondMessage = page.getByTestId("user-message").nth(1);
  await expect(secondMessage).toContainText(secondPrompt);
  await expect(secondMessage).toHaveAttribute("data-status", "offline", { timeout: 10_000 });
  await expect(secondMessage.locator(".status-offline")).toContainText("Queued offline");
  expect(await readBacklogPrompts(page)).toContain(secondPrompt);

  // Proof this went through the offline backlog, not the backend's online
  // per-session queue: that banner only appears on a real WS ack, which
  // never happened here.
  await expect(page.getByTestId("queued-prompt-banner")).toBeHidden();

  await page.context().setOffline(false);

  // The queued second prompt gets flushed and genuinely delivered.
  await expect(secondMessage).not.toHaveAttribute("data-status", "offline", { timeout: 30_000 });

  const assistantMessages = page.getByTestId("assistant-message");

  // The FIRST turn — never interrupted, never stopped, never affected by
  // the offline blip — completes on its own via WS reconnect + replay.
  await expect(stopBtn).toBeHidden({ timeout: 240_000 });
  await expect(assistantMessages.first()).toContainText("FIRSTDONE", { timeout: 240_000 });
  await expect(assistantMessages.first().locator(".stopped-indicator")).toHaveCount(0);

  // The SECOND (queued) prompt is delivered and answered afterward, in
  // order — index 1, not swapped with the first.
  await expect(page.getByTestId("user-message")).toHaveCount(2, { timeout: 30_000 });
  await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
  await expect(assistantMessages.nth(1)).toContainText("SECONDDONE", { timeout: 120_000 });

  // Durable backlog fully drained — no leftover trace of the queued action.
  await expect.poll(() => readBacklogPrompts(page)).not.toContain(secondPrompt);
});
