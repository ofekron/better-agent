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

test("approving one tool call lets the turn proceed to a second approval", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  // Settings is a distinct route from the app shell — createSessionWithPrompt
  // needs the sidebar's "+ New" button, which only exists on "/".
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run: echo FIRST_MARKER — then use the Bash tool again to run: echo SECOND_MARKER",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  const firstApprovalId = await approvalCard.getAttribute("data-approval-id");
  await approvalCard.locator(".user-input-card__actions button.primary").click();

  // The first card must be replaced by a distinct second approval card —
  // proof the turn asked for a second tool call rather than stopping after one.
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await expect
    .poll(async () => approvalCard.getAttribute("data-approval-id"), { timeout: 60_000 })
    .not.toBe(firstApprovalId);
  await expect(approvalCard).toContainText("SECOND_MARKER", { timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button.primary").click();

  await expect(page.getByTestId("assistant-message").last()).toContainText("FIRST_MARKER", {
    timeout: 60_000,
  });
  await expect(page.getByTestId("assistant-message").last()).toContainText("SECOND_MARKER", {
    timeout: 60_000,
  });
});

// ToolApprovalCard (Chat.tsx) already guards against a same-card double
// click with client-side `busy` state, so the real gap is server-side: does
// POST .../decide stay idempotent if a *second* real request reaches it for
// an approval the registry already resolved (races between browser tabs,
// retried requests, etc.)? tool_approval.py's `decide()` only resolves the
// pending record's Future once (`rec.future.done()` guards a second
// `set_result`), and `await_decision`'s `finally` pops the record out of
// `_pending` as soon as the first decision lands — so a second decide can
// land in one of two states, never a silent re-trigger:
//   - the record is already popped -> decide_tool_approval 404s
//   - decide() still finds it but sees future.done() -> 200 { ok: false }
// Either way the tool must not run twice.
test("a second decide on an already-decided approval is rejected, not re-run", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run exactly this command: echo DOUBLE_DECIDE_MARKER",
  );

  const sessionId = new URL(page.url()).pathname.replace(/^\/s\//, "");
  expect(sessionId).toBeTruthy();

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  const approvalId = await approvalCard.getAttribute("data-approval-id");
  expect(approvalId).toBeTruthy();

  const decideUrl = `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}/tool-approvals/${encodeURIComponent(approvalId!)}/decide`;

  // First decision: the real UI click, exactly like the happy-path test.
  await approvalCard.locator(".user-input-card__actions button.primary").click();
  await expect(approvalCard).not.toBeVisible({ timeout: 15_000 });

  // Second decision: a raw POST straight at the same approval id, as if a
  // second tab/request replayed the click after the first already resolved
  // it. This must NOT succeed a second time.
  const secondDecide = await page.request.post(decideUrl, { data: { approved: true } });
  if (secondDecide.status() === 404) {
    // Registry already popped the resolved record — object no longer exists.
    expect(secondDecide.ok()).toBe(false);
  } else {
    // Record still present but its Future is already resolved — decide()
    // must report it did not (re-)apply the decision.
    expect(secondDecide.status()).toBe(200);
    const body = await secondDecide.json();
    expect(body.ok).toBe(false);
  }

  // Whichever branch fired, the tool must have executed exactly once.
  const finalAssistantMessage = page.getByTestId("assistant-message").last();
  await expect(finalAssistantMessage).toContainText("DOUBLE_DECIDE_MARKER", { timeout: 60_000 });
  const fullText = await finalAssistantMessage.textContent();
  const occurrences = (fullText?.match(/DOUBLE_DECIDE_MARKER/g) ?? []).length;
  expect(occurrences).toBe(1);
});
