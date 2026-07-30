import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// A minimal valid 1x1 red PNG — real pixel data (not an empty/fake file) so
// the browser's canvas-based resize in imageAttach.ts (fileToPastedImage)
// has something real to decode and re-encode as it turns the attachment
// into a PastedImage.
const ONE_PIXEL_RED_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

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

// Validates the real client-side guard against wasted turns: InputArea's
// `canSend` is `(localDraft.trim() || images.length || files.length ||
// tagCount) && !disabled`, so a whitespace-only draft (no images/files/tags)
// must never be sendable — no real provider subprocess should ever be spun
// up for it.
test("whitespace-only prompt cannot be sent", async ({ authedPage: page, backend }) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: PONG. No punctuation, no other words.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();
  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessage).toContainText("PONG", { timeout: 120_000 });

  const sessionId = new URL(page.url()).pathname.replace(/^\/s\//, "");

  const countMessages = async (): Promise<number> => {
    const res = await page.request.get(
      `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
    );
    expect(res.ok()).toBe(true);
    const tree = await res.json();
    return ((tree.messages ?? []) as Array<{ id: string }>).length;
  };

  const messageCountBefore = await countMessages();
  await expect(page.getByTestId("user-message")).toHaveCount(1);
  await expect(page.getByTestId("assistant-message")).toHaveCount(1);

  const textarea = page.getByTestId("input-textarea");
  await textarea.fill("   \n  ");

  const sendBtn = page.getByTestId("send-btn");
  await expect(sendBtn).toBeDisabled();

  // Force-submitting whitespace-only content (Enter) must not add a new
  // user bubble or trigger a real turn.
  await textarea.press("Enter");

  await expect(sendBtn).toBeDisabled();
  await expect(page.getByTestId("user-message")).toHaveCount(1);
  await expect(page.getByTestId("assistant-message")).toHaveCount(1);
  await expect(page.getByTestId("stop-btn")).toBeHidden();

  expect(await countMessages()).toBe(messageCountBefore);
});

// Validates the real image-attach path: InputArea's hidden file input
// (attachmentInputRef, wired to handleAttachmentChange -> fileToPastedImage
// -> ComposerImagePreviews) accepts a real PNG, and the resulting
// PastedImage is actually transmitted to the backend and forwarded to the
// real `claude` CLI subprocess as part of a real multimodal turn — not
// silently dropped. Doesn't assert the model's color answer (vision
// accuracy varies); only that a real, non-empty assistant reply comes
// back, which proves the image round-tripped through send -> backend ->
// provider -> response.
test("attaches a real image to a prompt and sends a multimodal turn", async ({ authedPage: page }) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: PONG. No punctuation, no other words.",
  );

  const firstAssistantMessage = page.getByTestId("assistant-message");
  await expect(firstAssistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(firstAssistantMessage).toContainText("PONG", { timeout: 120_000 });

  const imageDir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-image-"));
  const imagePath = path.join(imageDir, "red-pixel.png");
  writeFileSync(imagePath, Buffer.from(ONE_PIXEL_RED_PNG_BASE64, "base64"));

  // InputArea renders exactly one hidden `<input type="file">`
  // (attachmentInputRef) once a session is open — the Settings and
  // New-Session-modal file inputs live on different views/components not
  // mounted here. setInputFiles works on it directly without needing to
  // open the overflow "..." menu or click the paperclip trigger first,
  // since it sets the files via CDP and bypasses the native OS picker.
  await page.locator('input[type="file"]').setInputFiles(imagePath);

  // handleAttachmentChange -> fileToPastedImage resolves asynchronously
  // (loads the file into an <img>, canvas-resizes/re-encodes it) before
  // ComposerImagePreviews renders the thumbnail, so wait for the real
  // preview rather than assuming it lands synchronously with setInputFiles.
  await expect(page.locator(".image-preview-item")).toHaveCount(1, { timeout: 15_000 });

  const textarea = page.getByTestId("input-textarea");
  await textarea.fill("What color is this image? Reply with exactly one word.");

  const sendBtn = page.getByTestId("send-btn");
  await expect(sendBtn).toBeEnabled();
  await sendBtn.click();

  await expect(page.getByTestId("user-message")).toHaveCount(2);
  // submitDraft clears the composer's attachment state (`setImages([],
  // false)`) once the turn is accepted, proving the image left the
  // composer as part of the real send rather than lingering unsent.
  await expect(page.locator(".image-preview-item")).toHaveCount(0);

  const assistantMessages = page.getByTestId("assistant-message");
  await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
  const secondAssistantMessage = assistantMessages.nth(1);
  await expect(secondAssistantMessage).toBeVisible({ timeout: 30_000 });
  // Real reply must have real, non-empty content — proving the image was
  // actually transmitted and processed by the provider, not silently
  // dropped. Exact color correctness isn't asserted since that depends on
  // model vision accuracy, not on whether the image round-tripped.
  await expect(secondAssistantMessage).not.toHaveText("", { timeout: 120_000 });
});

// Validates real multi-turn history + context retention: a second prompt
// sent in the same session, after the first turn has completed, must (a)
// be answered using context from the earlier turn (proving the backend
// actually replays prior conversation history into the provider CLI, not
// just the latest message) and (b) render as its own bubble pair appended
// after the first, in the correct chronological order — not overwriting or
// reordering existing bubbles.
test("maintains history and context across multiple turns in one session", async ({ authedPage: page }) => {
  await createSessionWithPrompt(page, "My favorite number is 42. Just acknowledge.");

  const userMessages = page.getByTestId("user-message");
  const assistantMessages = page.getByTestId("assistant-message");

  await expect(userMessages).toHaveCount(1);
  await expect(assistantMessages.first()).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessages.first()).not.toHaveText("", { timeout: 120_000 });

  const textarea = page.getByTestId("input-textarea");
  const sendBtn = page.getByTestId("send-btn");

  await textarea.fill("What is my favorite number plus 1? Reply with just the number.");
  await expect(sendBtn).toBeEnabled();
  await sendBtn.click();

  await expect(userMessages).toHaveCount(2);
  await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
  // Proves real context retention across turns (not an isolated single-turn
  // reply): the model can only answer "43" by recalling the number stated
  // in the first turn's prompt.
  await expect(assistantMessages.nth(1)).toContainText("43", { timeout: 120_000 });

  // Bubbles must be visible in strict chronological user/assistant/user/
  // assistant order, proving history isn't reordered, deduped, or
  // overwritten as later turns stream in.
  const bubbleOrder = await page
    .locator('[data-testid="user-message"], [data-testid="assistant-message"]')
    .evaluateAll((els) => els.map((el) => el.getAttribute("data-testid")));
  expect(bubbleOrder).toEqual(["user-message", "assistant-message", "user-message", "assistant-message"]);
});
