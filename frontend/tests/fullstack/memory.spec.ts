import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates the real agent-proposed memory feature: MemoryProposalCard
// (frontend/src/components/Chat.tsx), the approve/reject wiring through
// POST /api/user-input/{request_id}/resolve, and real persistence via
// memory_store.write_memory (backend/memory_store.py), verified end-to-end
// through the real, non-internal GET /api/memory/all route.
//
// IMPORTANT trigger caveat (read before "fixing" this test):
// There is currently no MCP tool wired to any provider that lets an agent
// naturally propose a memory (e.g. from "remember my favorite color is
// teal"). `backend/memory_api.py`'s docstring says the write route is
// "Called by the memory extension's MCP server after the user approves", but
// no such extension/tool exists yet in this repo (confirmed by searching
// every extensions/*/mcp server and every registered runtime operation, and
// by `git log`, which shows the memory extension as a still-open "WIP
// checkpoint"). So no prompt can make a real agent *decide* to propose a
// memory today -- the capability the agent would need doesn't exist to be
// discovered or called.
//
// To still exercise the real, non-mocked pipeline end-to-end, these tests
// have the real agent use its real Bash tool (already unauthenticated in
// the default fresh-provider permission mode) to POST directly to
// `/api/internal/user-input/request` with `kind: "memory"` -- the exact
// endpoint and payload shape the future MCP tool wrapper would call --
// using the real `BETTER_CLAUDE_BACKEND_URL` / `BETTER_CLAUDE_INTERNAL_TOKEN`
// / `BETTER_CLAUDE_APP_SESSION_ID` credentials already present in its own
// real process environment (see backend/open_file_panel_mcp.py for the
// identical pattern the sibling `request_user_input`/`request_user_approval`
// tools use). Everything downstream of that call -- the WS-pushed card, the
// approve/reject UI, the resolve endpoint, and the on-disk memory_store
// write -- is fully real. Once a real propose-memory tool ships, the prompt
// below should be replaced with a natural-language ask and this comment
// deleted.
//
// Because the trigger is a literal, copy-exact Bash command rather than a
// judgment call, the default fast model is expected to reproduce it
// reliably (mirrors approvals.spec.ts's "run exactly this command" pattern).
// The main remaining nondeterminism risk is the model paraphrasing/mangling
// the quoting instead of executing it verbatim.

function proposeMemoryCurlCommand(memory: {
  name: string;
  description: string;
  content: string;
}): string {
  // `$BETTER_CLAUDE_APP_SESSION_ID` stays inside its JSON string quotes --
  // the shell still expands it because the whole `-d` argument is
  // double-quoted (with the JSON's own quotes backslash-escaped below), and
  // double-quoted shell strings still perform `$VAR` expansion.
  const body = JSON.stringify({
    app_session_id: "$BETTER_CLAUDE_APP_SESSION_ID",
    kind: "memory",
    timeout_seconds: 60,
    memory_proposal: {
      action: "add",
      name: memory.name,
      description: memory.description,
      type: "user",
      content: memory.content,
      scope_type: "global",
      scope_path: "",
    },
  });
  const escapedBody = body.replace(/"/g, '\\"');
  return (
    `curl -sS -X POST "$BETTER_CLAUDE_BACKEND_URL/api/internal/user-input/request" ` +
    `-H "Content-Type: application/json" ` +
    `-H "X-Internal-Token: $BETTER_CLAUDE_INTERNAL_TOKEN" ` +
    `-d "${escapedBody}"`
  );
}

async function triggerMemoryProposal(
  page: import("@playwright/test").Page,
  memory: { name: string; description: string; content: string },
): Promise<void> {
  const command = proposeMemoryCurlCommand(memory);
  await createSessionWithPrompt(
    page,
    `Use the Bash tool to run exactly this single command, unmodified: ${command}`,
  );
}

test("approving an agent-proposed memory persists it to the real memory store", async ({
  authedPage: page,
  backend,
}) => {
  await triggerMemoryProposal(page, {
    name: "favorite-color",
    description: "The user's favorite color.",
    content: "The user's favorite color is teal.",
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  const nameInput = card.locator(".memory-proposal-card__field input").first();
  await expect(nameInput).toHaveValue("favorite-color");
  await expect(card.locator(".memory-proposal-card__content")).toHaveValue(
    "The user's favorite color is teal.",
  );

  // Button order in Chat.tsx's MemoryProposalCard: Expand, Reject, Approve
  // (Approve carries the `primary` class, same convention approvals.spec.ts
  // relies on for the tool-approval card).
  await card.locator(".user-input-card__actions button.primary").click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;
  const persisted = globalMemories.find((m) => m.name === "favorite-color");
  expect(persisted).toBeDefined();
  expect(persisted?.content).toContain("teal");
});

test("rejecting an agent-proposed memory does not persist it", async ({
  authedPage: page,
  backend,
}) => {
  await triggerMemoryProposal(page, {
    name: "least-favorite-color",
    description: "The user's least favorite color.",
    content: "The user's least favorite color is beige.",
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  // nth(1): Expand (0), Reject (1), Approve (2, `.primary`).
  await card.locator(".user-input-card__actions button").nth(1).click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string }>;
  expect(globalMemories.some((m) => m.name === "least-favorite-color")).toBe(false);
});
