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

// Full-stack regression for the exact production failure: a finalized
// assistant message with no event preview fell through to the plain summary
// linkifier, leaving Markdown markers visible while file references became
// chips. The backend, auth flow, production bundle, browser, and session API
// are real; only the persisted response payload is shaped deterministically
// at the network boundary so this proof does not spend an LLM turn.
test("renders Markdown in a finalized response with no event preview", async ({
  authedPage: page,
  backend,
}) => {
  const create = await page.request.post(`${backend.baseURL}/api/sessions`, {
    data: { name: "Markdown fallback regression", cwd: "/tmp" },
  });
  expect(create.ok()).toBe(true);
  const session = (await create.json()) as { id: string };
  const sessionUrl = new URL(`/s/${session.id}`, backend.baseURL).toString();

  await page.route(`**/api/sessions/${encodeURIComponent(session.id)}*`, async (route) => {
    const response = await route.fetch();
    const tree = (await response.json()) as Record<string, unknown>;
    tree.messages = [
      {
        id: "u-markdown-fallback",
        role: "user",
        content: "show result",
        events: [],
        timestamp: "2026-08-02T20:00:00.000Z",
        isStreaming: false,
      },
      {
        id: "a-markdown-fallback",
        role: "assistant",
        content: [
          "## Executive summary",
          "",
          "**Goal:** keep the result readable",
          "",
          "```text",
          "a75a133a6",
          "```",
          "",
          "[MessageBubble.tsx](bcfile:%2Ftmp%2FMessageBubble.tsx)",
        ].join("\n"),
        events: [],
        timestamp: "2026-08-02T20:00:01.000Z",
        isStreaming: false,
      },
    ];
    tree.total_messages = 2;
    await route.fulfill({ response, body: JSON.stringify(tree) });
  });

  await page.goto(sessionUrl);
  await expect(page.getByTestId("user-message")).toBeVisible();
  const summary = page.locator(".collapse-summary");
  await expect(summary).toBeVisible();
  await expect(summary.locator("h2")).toHaveText("Executive summary");
  await expect(summary.locator("strong")).toHaveText("Goal:");
  await expect(summary.locator("code")).toHaveText("a75a133a6");
  await expect(summary).not.toContainText("## Executive summary");
  await expect(summary).not.toContainText("**Goal:**");
  await expect(summary).not.toContainText("```text");
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

// Validates that interrupting a turn fully resets composer state: after
// stopping an in-flight turn, InputArea must be immediately usable again for
// a brand-new prompt, not left disabled/wedged by stale `isStreaming` or
// pending-interrupt state. Unlike the plain interrupt test above, this does
// NOT wait for the stopped-indicator or any other post-interrupt settling —
// it types and sends the next prompt the instant `stop-btn` disappears, to
// prove the composer resets synchronously with the interrupt rather than
// after some later cleanup step.
test("composer is immediately usable for a new prompt after interrupting a turn", async ({ authedPage: page }) => {
  await createSessionWithPrompt(
    page,
    "Count from 1 to 100 slowly, one number per line, explaining each number's factors.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  const stopBtn = page.getByTestId("stop-btn");
  await expect(stopBtn).toBeVisible({ timeout: 30_000 });

  await stopBtn.click();
  await expect(stopBtn).toBeHidden({ timeout: 30_000 });

  // No further waiting: immediately drive a brand-new prompt through the
  // real composer to prove it isn't stuck disabled or otherwise wedged.
  const textarea = page.getByTestId("input-textarea");
  const newPrompt = "Reply with exactly the single word: RESUMED. No punctuation, no other words.";
  await textarea.fill(newPrompt);

  const sendBtn = page.getByTestId("send-btn");
  await expect(sendBtn).toBeEnabled();
  await sendBtn.click();

  // A real second user bubble plus a real, content-bearing second assistant
  // reply proves the interrupt path left the composer and turn pipeline
  // fully functional, not merely visually reset.
  await expect(page.getByTestId("user-message")).toHaveCount(2);
  const assistantMessages = page.getByTestId("assistant-message");
  await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
  await expect(assistantMessages.nth(1)).toContainText("RESUMED", { timeout: 120_000 });
});

// Validates the real online (same-session) queue cancel path, distinct from
// the offline queue: a prompt queued via the send-btn while a turn is
// running (same mechanism as the "queues a second prompt" test above) can be
// discarded via the real QueuedPromptBanner cancel control BEFORE the
// in-flight turn finishes and the backend's per-session queue would
// otherwise auto-dequeue it. Cancelling routes through App.tsx's
// `handleCancelQueued` -> a real `ConfirmModal` confirmation (cancelling a
// queued prompt permanently discards user-authored text, so it is gated
// behind an explicit confirm) -> `confirmCancelQueued`, which is the real
// production guard, not a test shortcut.
test("cancels a queued prompt before it runs", async ({ authedPage: page, backend }) => {
  await createSessionWithPrompt(
    page,
    "Count from 1 to 50 slowly, one number per line, explaining each number's factors.",
  );

  await expect(page.getByTestId("user-message")).toBeVisible();

  const sessionId = new URL(page.url()).pathname.replace(/^\/s\//, "");
  const countMessages = async (): Promise<number> => {
    const res = await page.request.get(
      `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
    );
    expect(res.ok()).toBe(true);
    const tree = await res.json();
    return ((tree.messages ?? []) as Array<{ id: string }>).length;
  };

  // Same event-driven proxy for "the turn is actually running" as the
  // interrupt/queue tests above.
  const stopBtn = page.getByTestId("stop-btn");
  await expect(stopBtn).toBeVisible({ timeout: 30_000 });

  // Baseline captured while the first turn is in flight but before the
  // second prompt is ever queued. The backend (turn_manager.py's
  // Coordinator.run_turn) eagerly persists an assistant placeholder row
  // BEFORE driving the CLI subprocess — the same eager-persist that makes
  // `stop-btn`'s visibility a reliable "turn running" signal also means
  // this count already reflects its final level (user1 + assistant1): the
  // provider only fills in that existing row in place as it streams, it
  // never appends a new one on completion.
  const messageCountBeforeQueue = await countMessages();

  const queuedPromptText = "Reply with exactly the single word: NEVERSENT. No punctuation, no other words.";
  await page.getByTestId("input-textarea").fill(queuedPromptText);
  await page.getByTestId("send-btn").click();

  const queuedBanner = page.getByTestId("queued-prompt-banner");
  await expect(queuedBanner).toBeVisible({ timeout: 15_000 });
  await expect(queuedBanner).toContainText("NEVERSENT");

  // The real cancel control on the (non-minimized, single-item) banner —
  // InputArea.tsx renders it as a plain `.queued-cancel-btn` button (no
  // dedicated data-testid; that's reserved for the bulk `queued-bulk-cancel`
  // variant used when 2+ prompts are queued) that fires `onCancel` ->
  // App.tsx's `handleCancelQueued`.
  await queuedBanner.locator(".queued-cancel-btn").click();

  // Cancelling a queued prompt is gated behind a real ConfirmModal
  // ("Discard queued prompt?") since it permanently discards user-authored
  // text — click its real confirm action, not a bypass. ConfirmModal.tsx
  // has no data-testid/role="dialog" of its own, so scope by its
  // `.modal-content` class and the exact translated confirm label
  // (`queued.cancelConfirmAction_one` = "Discard prompt").
  const confirmDialog = page.locator(".modal-content", { hasText: "Discard queued prompt?" });
  await expect(confirmDialog).toBeVisible({ timeout: 10_000 });
  await confirmDialog.getByRole("button", { name: "Discard prompt" }).click();

  // The queued banner is gone and the never-sent text never became its own
  // user bubble, all BEFORE the first turn has been allowed to finish.
  await expect(queuedBanner).toBeHidden({ timeout: 15_000 });
  await expect(page.getByTestId("user-message")).toHaveCount(1);
  await expect(page.getByText("NEVERSENT")).toHaveCount(0);

  // Now let the first (still in-flight) turn complete.
  await expect(stopBtn).toBeHidden({ timeout: 180_000 });
  const assistantMessages = page.getByTestId("assistant-message");
  await expect(assistantMessages.first()).not.toHaveText("", { timeout: 15_000 });

  // Final state proves the cancelled prompt was never sent or replied to:
  // exactly one user/assistant pair, and the backend's message count is
  // unchanged from the pre-queue baseline — the eagerly-persisted
  // assistant row was only filled in place, and the cancelled prompt never
  // became a second turn (which would append two more rows).
  await expect(page.getByTestId("user-message")).toHaveCount(1);
  await expect(assistantMessages).toHaveCount(1);
  await expect(page.getByText("NEVERSENT")).toHaveCount(0);
  expect(await countMessages()).toBe(messageCountBeforeQueue);
});

// Validates the real long-input path end to end: a large (5000+ char) but
// genuine prompt — real repeated paragraph text, not a synthetic
// filler/padding string — must be accepted by the composer, transmitted in
// full to the backend, forwarded whole to the real `claude` CLI subprocess,
// and rendered without truncation. The prompt ends with a distinguishing
// instruction the model can only satisfy by actually reading to the end of
// the (very long) input, so a correct "LONGINPUT_OK" reply proves the full
// payload round-tripped through send -> backend -> provider -> response
// rather than being silently truncated somewhere in the pipeline.
test("accepts, sends, and displays a very long prompt without truncation", async ({ authedPage: page }) => {
  const paragraph =
    "The quick brown fox jumps over the lazy dog near the riverbank while " +
    "the autumn leaves drift slowly across the quiet meadow at dusk. ";
  const fillerInstruction =
    "Ignore all of the above filler text and just reply with exactly: LONGINPUT_OK";
  let longPrompt = paragraph.repeat(Math.ceil(5000 / paragraph.length) + 5);
  longPrompt = `${longPrompt}\n\n${fillerInstruction}`;
  expect(longPrompt.length).toBeGreaterThan(5000);

  await createSessionWithPrompt(page, longPrompt);

  const userMessage = page.getByTestId("user-message");
  await expect(userMessage).toBeVisible();
  // The bubble must retain the ending instruction verbatim — proving the
  // composer/backend didn't silently truncate the (very long) middle or
  // tail of the prompt before it was ever sent or rendered.
  await expect(userMessage).toContainText(fillerInstruction);

  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessage).toContainText("LONGINPUT_OK", { timeout: 120_000 });
});

// Trust/accuracy check, in the spirit of the approval-command-accuracy test
// in approvals.spec.ts: the run-meta chips on an assistant bubble
// (MessageBubble.tsx's `RunMetaChips`, rendered inside `AssistantRunMeta`
// as `.run-meta-chip` spans with a `.run-meta-chip-value` per fact) are the
// user's only visible signal for which provider/model actually answered a
// given turn. If they showed a stale or wrong identity, the user would be
// misled about what actually ran. Assert the chips match the REAL backend
// truth — this exact message's persisted `run_meta.provider_id`/`model`
// (session_manager.py stamps this from what the provider CLI actually ran
// with) and the matching `/api/providers` record's display name — fetched
// independently via REST, not assumed from client-side defaults.
test("the run-meta provider/model chips reflect the real backend truth for the turn", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(
    page,
    "Reply with exactly the single word: PONG. No punctuation, no other words.",
  );

  const assistantMessage = page.getByTestId("assistant-message");
  await expect(assistantMessage).toBeVisible({ timeout: 30_000 });
  await expect(assistantMessage).toContainText("PONG", { timeout: 120_000 });

  const sessionId = new URL(page.url()).pathname.replace(/^\/s\//, "");
  const treeRes = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
  );
  expect(treeRes.ok()).toBe(true);
  const tree = await treeRes.json();
  const messages = (tree.messages ?? []) as Array<{
    role: string;
    run_meta?: { provider_id?: string; model?: string };
  }>;
  const assistantRecord = [...messages].reverse().find((m) => m.role === "assistant");
  const runMeta = assistantRecord?.run_meta;
  // The turn actually ran (PONG rendered above), so the backend must have
  // stamped a real provider/model onto this message — not an empty scaffold.
  expect(runMeta?.provider_id).toBeTruthy();
  expect(runMeta?.model).toBeTruthy();

  // Runtime-profiles reality: the session was created through the default
  // runtime profile (the modal's profile picker pre-selects it), so the
  // persisted session record must carry that profile's id, and the profile
  // must be live and back the same provider the turn actually ran with —
  // the run-meta chips and the profile identity may never disagree.
  const profilesRes = await page.request.get(`${backend.baseURL}/api/runtime-profiles`);
  expect(profilesRes.ok()).toBe(true);
  const profilesBody = (await profilesRes.json()) as {
    runtime_profiles: Array<{ id: string; provider_id: string; deleted_at: string | null }>;
    default_runtime_profile_id: string | null;
  };
  expect(tree.runtime_profile_id).toBeTruthy();
  expect(tree.runtime_profile_id).toBe(profilesBody.default_runtime_profile_id);
  const backingProfile = profilesBody.runtime_profiles.find(
    (p) => p.id === tree.runtime_profile_id,
  );
  expect(backingProfile).toBeTruthy();
  expect(backingProfile!.deleted_at).toBeNull();
  expect(backingProfile!.provider_id).toBe(runMeta!.provider_id);

  const providersRes = await page.request.get(`${backend.baseURL}/api/providers`);
  expect(providersRes.ok()).toBe(true);
  const providersBody = await providersRes.json();
  const providers = (providersBody.providers ?? []) as Array<{
    id: string;
    name: string;
    nickname?: string;
  }>;
  const provider = providers.find((p) => p.id === runMeta!.provider_id);
  expect(provider).toBeTruthy();
  // Mirrors providerDisplayName.ts (the frontend's single source of truth
  // for provider labeling): base name, plus " · nickname" when set.
  const expectedProviderName = provider!.nickname
    ? `${provider!.name} · ${provider!.nickname}`
    : provider!.name;

  const chipValues = assistantMessage.locator(".run-meta-chip-value");
  await expect(chipValues.filter({ hasText: expectedProviderName })).toBeVisible();
  await expect(chipValues.filter({ hasText: runMeta!.model! })).toBeVisible();
});
