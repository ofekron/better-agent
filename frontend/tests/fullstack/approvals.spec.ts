import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import { addProjectByPath } from "./harness/projects";
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

// Regression guard for the permission "mode" axis getting stuck once a
// session has already been through an approval-gated turn: flips mode to
// "Default (prompt)", drives one real approval-gated turn, flips mode back
// to "Bypass all permissions" and saves, then drives a second real turn and
// asserts the approval card never appears — proof the mode is read live per
// turn from the provider's current settings, not cached/latched from the
// first turn.
test("switching back to bypass mid-session stops future turns from prompting", async ({
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
    "Use the Bash tool to run exactly this command: echo BYPASS_REGRESSION_FIRST_MARKER",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button.primary").click();

  await expect(page.getByTestId("assistant-message").last()).toContainText(
    "BYPASS_REGRESSION_FIRST_MARKER",
    { timeout: 60_000 },
  );

  // Flip the mode back to full bypass now that one approval-gated turn has
  // already happened, and confirm it actually takes effect for the next turn.
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Bypass all permissions");
  await saveProviderSettings(page);
  await page.goto(backend.baseURL);

  await createSessionWithPrompt(
    page,
    "Use the Bash tool to run exactly this command: echo BYPASS_REGRESSION_SECOND_MARKER",
  );

  await expect(page.getByTestId("assistant-message").last()).toContainText(
    "BYPASS_REGRESSION_SECOND_MARKER",
    { timeout: 60_000 },
  );
  await expect(approvalCard).not.toBeVisible();
});

// Trust/accuracy check: the user approves based on what the card SHOWS them,
// so the card must render the real command, not a generic "Bash tool call"
// placeholder. ToolApprovalCard (Chat.tsx) builds its argument rows from
// toolApprovalArgRows(approval.summary), which reads summary.input (or the
// legacy summary.args) and renders each entry as a
// `.tool-approval-card__arg-value` <dd> keyed by `.tool-approval-card__arg-key`
// <dt> — for the Bash tool that's the `command` key holding the exact shell
// string. Assert the visible card text contains the distinctive command
// verbatim, before it's approved.
test("the approval card displays the real command, not a placeholder", async ({
  authedPage: page,
  backend,
}) => {
  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  // Settings is a distinct route from the app shell — createSessionWithPrompt
  // needs the sidebar's "+ New" button, which only exists on "/".
  await page.goto(backend.baseURL);

  const command = "echo UNIQUE_VISIBLE_MARKER_XYZ";
  await createSessionWithPrompt(page, `Use the Bash tool to run exactly this command: ${command}`);

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });

  // The full, exact command string must be visible verbatim — not truncated,
  // not paraphrased, not swapped for a generic "run a shell command" message.
  await expect(approvalCard).toContainText(command, { timeout: 15_000 });

  // Pin down which field carries it: the `command` arg row's value cell.
  const commandValue = approvalCard.locator(".tool-approval-card__arg-value", {
    hasText: "UNIQUE_VISIBLE_MARKER_XYZ",
  });
  await expect(commandValue).toBeVisible();
  await expect(commandValue).toHaveText(command);

  await approvalCard.locator(".user-input-card__actions button.primary").click();

  await expect(page.getByTestId("assistant-message").last()).toContainText(
    "UNIQUE_VISIBLE_MARKER_XYZ",
    { timeout: 60_000 },
  );
});

// The previous denial test only proves the tool didn't run. It doesn't prove
// the model actually *saw* the denial as feedback in the same turn, versus
// the turn just silently stopping/hanging after a blocked call. Ask the
// model for an explicit fallback if denied, deny the card, and require the
// fallback text to show up — that only happens if the denial was delivered
// back into the conversation and the model reacted to it.
test("a denied tool approval is delivered back to the model, which can course-correct", async ({
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
    "Use the Bash tool to run: echo FIRST_ATTEMPT — if that's denied, instead just reply with the word FALLBACK_USED with no tool calls",
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });
  await approvalCard.locator(".user-input-card__actions button:not(.primary)").click();

  // Proof the deny path informs the model rather than just silently blocking:
  // the same turn must produce a real reply containing the requested
  // fallback text, not hang or end with nothing.
  await expect(page.getByTestId("assistant-message").last()).toContainText("FALLBACK_USED", {
    timeout: 60_000,
  });
});

// Every previous test in this file gates a Bash call. toolApprovalArgRows
// (Chat.tsx) is written generically — it reads whatever keys are present in
// summary.input and renders one row per key, with no Bash-specific
// branching — so a file-write tool (Write) should hit the exact same
// approval-card path with its own keys (file_path/content) instead of a
// Bash-shaped `command` row. This proves that generalization holds for a
// real, different tool type, not just Bash under a different prompt.
//
// A fresh session's cwd defaults to the backend process's real Path.home(),
// not an isolated dir, so we register a controlled workspace as a project
// first (the same addProjectByPath flow file-editor.spec.ts uses) and let
// the new-session modal pick it up as the default cwd — giving the model's
// Write call a real, writable, known location to target.
test("a non-Bash tool (Write) also triggers a real approval card", async ({
  authedPage: page,
  backend,
}) => {
  const workspaceDir = path.join(backend.homeDir, "workspace-write-approval");
  mkdirSync(workspaceDir, { recursive: true });
  const fileName = "approval_test.txt";
  const filePath = path.join(workspaceDir, fileName);

  await openProviderSettings(page, backend.baseURL, "claude");
  await pickCustomSelectOption(page, "permission-axis-select-mode", "Default (prompt)");
  await saveProviderSettings(page);
  // Settings is a distinct route from the app shell — createSessionWithPrompt
  // needs the sidebar's "+ New" button, which only exists on "/".
  await page.goto(backend.baseURL);

  await addProjectByPath(page, workspaceDir);

  await createSessionWithPrompt(
    page,
    `Create a file named ${fileName} with the content HELLO using your file write tool.`,
  );

  const approvalCard = page.getByTestId("tool-approval-card");
  await expect(approvalCard).toBeVisible({ timeout: 60_000 });

  // Proof this isn't the Bash path: no `command` arg row, and the tool name
  // header names the file-write tool, not Bash.
  await expect(
    approvalCard.locator(".tool-approval-card__arg-key", { hasText: "command" }),
  ).toHaveCount(0);
  await expect(approvalCard).not.toContainText(/^Bash$/);

  // toolApprovalArgRows renders every key in summary.input generically, so
  // the file path and/or content the model is about to write should be
  // visible verbatim before approval — the same trust guarantee L5 locks in
  // for Bash's `command` key, generalized to Write's own keys.
  await expect(approvalCard).toContainText(fileName, { timeout: 15_000 });

  await approvalCard.locator(".user-input-card__actions button.primary").click();
  await expect(approvalCard).not.toBeVisible({ timeout: 15_000 });

  // The real proof: the file actually got created on disk with that
  // content, not just a DOM assertion that the card disappeared.
  await expect.poll(() => readFileSync(filePath, "utf-8").trim(), { timeout: 60_000 }).toBe(
    "HELLO",
  );
});
