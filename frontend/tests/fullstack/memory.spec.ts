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
  scope_type?: "global" | "project" | "folder";
  scope_path?: string;
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
      scope_type: memory.scope_type ?? "global",
      scope_path: memory.scope_path ?? "",
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
  memory: {
    name: string;
    description: string;
    content: string;
    scope_type?: "global" | "project" | "folder";
    scope_path?: string;
  },
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

test("editing a proposed memory's fields before approving persists the edited values", async ({
  authedPage: page,
  backend,
}) => {
  await triggerMemoryProposal(page, {
    name: "favorite-drink",
    description: "The user's favorite drink.",
    content: "The user's favorite drink is coffee.",
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  const nameInput = card.locator(".memory-proposal-card__field input").first();
  await expect(nameInput).toHaveValue("favorite-drink");
  const contentField = card.locator(".memory-proposal-card__content");
  await expect(contentField).toHaveValue("The user's favorite drink is coffee.");

  await nameInput.fill("favorite-beverage");
  await contentField.fill("The user's favorite drink is actually tea.");

  await card.locator(".user-input-card__actions button.primary").click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;

  expect(globalMemories.some((m) => m.name === "favorite-drink")).toBe(false);

  const persisted = globalMemories.find((m) => m.name === "favorite-beverage");
  expect(persisted).toBeDefined();
  expect(persisted?.content).toContain("tea");
  expect(persisted?.content).not.toContain("coffee");
});

test("two memory proposals in the same session are approved and rejected independently", async ({
  authedPage: page,
  backend,
}) => {
  // Turn 1: create the session and propose the first memory via the shared
  // helper (which drives the real "+ New" flow).
  await triggerMemoryProposal(page, {
    name: "favorite-season",
    description: "The user's favorite season.",
    content: "The user's favorite season is autumn.",
  });

  const firstCard = page.getByTestId("memory-proposal-card");
  await expect(firstCard).toBeVisible({ timeout: 60_000 });
  await expect(firstCard.locator(".memory-proposal-card__field input").first()).toHaveValue(
    "favorite-season",
  );

  await firstCard.locator(".user-input-card__actions button.primary").click();
  await expect(firstCard).not.toBeVisible({ timeout: 15_000 });

  // Turn 2: same session, second real turn -- send a follow-up prompt
  // through the chat input (createSessionWithPrompt always opens a *new*
  // session via "+ New", so a plain textarea send is what keeps this in the
  // same session) proposing a second, distinct memory.
  const secondCommand = proposeMemoryCurlCommand({
    name: "least-favorite-season",
    description: "The user's least favorite season.",
    content: "The user's least favorite season is winter.",
  });
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(
    `Use the Bash tool to run exactly this single command, unmodified: ${secondCommand}`,
  );
  await textarea.press("Enter");

  const secondCard = page.getByTestId("memory-proposal-card");
  await expect(secondCard).toBeVisible({ timeout: 60_000 });
  await expect(secondCard.locator(".memory-proposal-card__field input").first()).toHaveValue(
    "least-favorite-season",
  );

  // nth(1): Expand (0), Reject (1), Approve (2, `.primary`).
  await secondCard.locator(".user-input-card__actions button").nth(1).click();
  await expect(secondCard).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;

  // The approved first proposal persisted, unaffected by the second turn's
  // rejection...
  const approved = globalMemories.find((m) => m.name === "favorite-season");
  expect(approved).toBeDefined();
  expect(approved?.content).toContain("autumn");

  // ...and the rejected second proposal did not, unaffected by the first
  // turn's approval.
  expect(globalMemories.some((m) => m.name === "least-favorite-season")).toBe(false);
});

test("approving a project-scoped memory with a nonexistent scope_path is accepted and stored verbatim, not silently corrupting the store", async ({
  authedPage: page,
  backend,
}) => {
  // memory_store.write_memory (backend/memory_store.py) never checks that
  // `scope_path` refers to a real, existing directory -- `scope_dir()` just
  // lexically resolves it (Path.resolve() doesn't require the path to
  // exist) and hashes it via `encode_cwd`, same as any real project path.
  // `memory_api.validate_memory_proposal` only checks scope_path is
  // non-empty/single-line/<=1000 chars for scope_type "project"/"folder" --
  // it never stats the filesystem either. So this is expected to succeed,
  // not be rejected, with the scope_path preserved verbatim in `_scope.json`
  // (read back here through the real, non-internal GET /api/memory/all
  // route's `scopes` array) rather than silently dropped or corrupting the
  // global/other-scope stores.
  const fakeScopePath = "/nonexistent/fake/path/for/fullstack-test";

  await triggerMemoryProposal(page, {
    name: "bogus-scope-memory",
    description: "A memory proposed against a scope_path that doesn't exist.",
    content: "This memory is scoped to a project directory that was never created.",
    scope_type: "project",
    scope_path: fakeScopePath,
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  const nameInput = card.locator(".memory-proposal-card__field input").first();
  await expect(nameInput).toHaveValue("bogus-scope-memory");

  await card.locator(".user-input-card__actions button.primary").click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();

  // Not silently dropped into global, and the global store isn't corrupted
  // by a project-scoped write.
  const globalMemories = memoriesBody.global as Array<{ name: string }>;
  expect(globalMemories.some((m) => m.name === "bogus-scope-memory")).toBe(false);

  // Accepted as a real scope with the invalid path stored verbatim.
  const scopes = memoriesBody.scopes as Array<{
    type: string;
    path: string;
    memories: Array<{ name: string; content: string }>;
  }>;
  const fakeScope = scopes.find((s) => s.type === "project" && s.path === fakeScopePath);
  expect(fakeScope).toBeDefined();
  const persisted = fakeScope?.memories.find((m) => m.name === "bogus-scope-memory");
  expect(persisted).toBeDefined();
  expect(persisted?.content).toContain("never created");
});

test("a memory's content with unicode, embedded quotes, newlines, and markdown-shaped text round-trips byte-for-byte", async ({
  authedPage: page,
  backend,
}) => {
  // Exercises the full propose -> approve -> persist -> re-fetch pipeline
  // with content designed to break naive escaping at any layer: real
  // multi-byte unicode (accented letters, an emoji outside the BMP, CJK
  // text), an embedded double-quote (the same character
  // `proposeMemoryCurlCommand` and the shell command wrapper both use as
  // their delimiter), a literal newline, and markdown-looking syntax that
  // could be misinterpreted by a naive sanitizer. `proposeMemoryCurlCommand`
  // already JSON.stringify()s the payload (which turns a literal newline
  // into the two-character escape `\n` and leaves unicode as real UTF-8
  // characters) before backslash-escaping every `"` for the outer
  // double-quoted shell string -- reused unmodified here, since that
  // generic recipe is exactly what's under test.
  const specialContent =
    'Café ☕ 日本語 memory content with a "quoted phrase", an emoji 🎉, ' +
    'markdown-looking text like **bold**, _italic_, `code`, and a literal\nnewline right here.';

  await triggerMemoryProposal(page, {
    name: "special-characters-round-trip",
    description: "Memory content with unicode, quotes, newlines, and markdown syntax.",
    content: specialContent,
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  const nameInput = card.locator(".memory-proposal-card__field input").first();
  await expect(nameInput).toHaveValue("special-characters-round-trip");
  // Exact match (not `.toContain`) on the proposal card itself: confirms the
  // content survived the curl -> internal user-input request -> WS-pushed
  // card round trip with no mangling before approval even happens.
  await expect(card.locator(".memory-proposal-card__content")).toHaveValue(specialContent);

  await card.locator(".user-input-card__actions button.primary").click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;
  const persisted = globalMemories.find((m) => m.name === "special-characters-round-trip");
  expect(persisted).toBeDefined();
  // Exact, byte-for-byte equality on the on-disk-persisted, re-fetched
  // content -- not `.toContain` -- to catch truncation, double-escaping
  // (e.g. `\"` becoming `\\"`, or `\n` becoming a literal two-char
  // backslash-n instead of a real newline), or any corruption of the
  // unicode bytes anywhere in the propose/approve/persist/re-fetch chain.
  expect(persisted?.content).toBe(specialContent);
});

// No dedicated "view persisted memories" UI exists in this repo: searching
// frontend/src/components turns up MemoryProposalCard (Chat.tsx) for the
// propose/approve flow and nothing else -- no MemoryList/MemoriesPage
// component, and SettingsPage.tsx's nav `sections` list (providers,
// language, account, appearance, desktop, recovery, shortcuts, delegation,
// context, internalLlm, sessions, voice, extensions, capabilities,
// harnessProfiles, passwords, server, notifications) has no memory/memories
// entry. `grep -rn "memory/all" frontend/src` finds zero references outside
// this test file -- nothing in the frontend fetches it. The only consumer of
// `GET /api/memory/all` (backend/main.py) is this test suite; its docstring
// even says "for the settings memory browser", but that browser was never
// built. So instead of a UI assertion, this test exercises the REST
// contract directly: propose+approve one global-scope and one
// project-scope memory in the same session, then assert `GET
// /api/memory/all` buckets each into the correct top-level key (`global`
// vs `scopes`) without cross-contaminating the other bucket.
test("GET /api/memory/all correctly buckets a mix of global and project-scoped memories", async ({
  authedPage: page,
  backend,
}) => {
  const scopePath = "/nonexistent/fake/path/for/fullstack-mix-test";

  // Turn 1: propose and approve a global-scope memory.
  await triggerMemoryProposal(page, {
    name: "global-mix-memory",
    description: "A global-scope memory for the bucketing test.",
    content: "This memory belongs in the global bucket.",
  });

  const firstCard = page.getByTestId("memory-proposal-card");
  await expect(firstCard).toBeVisible({ timeout: 60_000 });
  await expect(firstCard.locator(".memory-proposal-card__field input").first()).toHaveValue(
    "global-mix-memory",
  );
  await firstCard.locator(".user-input-card__actions button.primary").click();
  await expect(firstCard).not.toBeVisible({ timeout: 15_000 });

  // Turn 2: same session, propose and approve a project-scope memory.
  const secondCommand = proposeMemoryCurlCommand({
    name: "project-mix-memory",
    description: "A project-scope memory for the bucketing test.",
    content: "This memory belongs in the scopes bucket.",
    scope_type: "project",
    scope_path: scopePath,
  });
  const textarea = page.getByTestId("input-textarea");
  await textarea.fill(
    `Use the Bash tool to run exactly this single command, unmodified: ${secondCommand}`,
  );
  await textarea.press("Enter");

  const secondCard = page.getByTestId("memory-proposal-card");
  await expect(secondCard).toBeVisible({ timeout: 60_000 });
  await expect(secondCard.locator(".memory-proposal-card__field input").first()).toHaveValue(
    "project-mix-memory",
  );
  await secondCard.locator(".user-input-card__actions button.primary").click();
  await expect(secondCard).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();

  // The global memory is bucketed under `global`, not leaked into `scopes`.
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;
  const globalEntry = globalMemories.find((m) => m.name === "global-mix-memory");
  expect(globalEntry).toBeDefined();
  expect(globalEntry?.content).toContain("global bucket");
  expect(globalMemories.some((m) => m.name === "project-mix-memory")).toBe(false);

  // The project-scoped memory is bucketed under `scopes`, under its own
  // scope entry, not leaked into `global`.
  const scopes = memoriesBody.scopes as Array<{
    type: string;
    path: string;
    memories: Array<{ name: string; content: string }>;
  }>;
  const scopeEntry = scopes.find((s) => s.type === "project" && s.path === scopePath);
  expect(scopeEntry).toBeDefined();
  const scopedMemory = scopeEntry?.memories.find((m) => m.name === "project-mix-memory");
  expect(scopedMemory).toBeDefined();
  expect(scopedMemory?.content).toContain("scopes bucket");
  expect(scopeEntry?.memories.some((m) => m.name === "global-mix-memory")).toBe(false);
});

// MemoryProposalCard (frontend/src/components/Chat.tsx) renders its fields
// twice: once inline in the card itself (the `.memory-proposal-card__content`
// textarea there is fixed at `rows={4}`), and once inside a
// `.memory-proposal-modal__overlay` modal that only mounts when `expanded`
// state is true. Clicking "Expand" (the first, non-primary button in the
// base card's `.user-input-card__actions` -- it has no click handler that
// touches `resolve()`, only `setExpanded(true)`) mounts that modal, which
// re-renders the identical fields in a `rows={18}` textarea -- room to read
// far more of a long memory's content at once without scrolling, before the
// user commits to approve/reject. This test proves: (1) Expand reveals that
// larger, previously-absent modal view rather than mutating the base card in
// place, (2) it does not call `resolve()` -- the request stays open, the
// card stays mounted, no POST to the resolve endpoint fires -- and (3) the
// normal Approve flow afterward still persists correctly, i.e. expanding
// first has no side effect on the eventual outcome.
test("expanding a memory proposal reveals the full-content modal without resolving the card, and approving afterward still persists it", async ({
  authedPage: page,
  backend,
}) => {
  // Ten lines: far more than the base card's 4-row textarea can show without
  // scrolling, so the modal's 18-row textarea genuinely surfaces content that
  // was scrolled out of view a moment ago.
  const multilineContent = Array.from(
    { length: 10 },
    (_, i) => `Line ${i + 1} of the user's expand-test memory content.`,
  ).join("\n");

  await triggerMemoryProposal(page, {
    name: "expand-test-memory",
    description: "A memory whose content is long enough to require expanding.",
    content: multilineContent,
  });

  const card = page.getByTestId("memory-proposal-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  // Scope to the base card's own action row (document order: this is the
  // first `.user-input-card__actions` -- the modal's duplicate one, if any,
  // would come after it once mounted).
  const baseActions = card.locator(".user-input-card__actions").first();
  const baseContent = card.locator(".memory-proposal-card__content").first();
  await expect(baseContent).toHaveAttribute("rows", "4");
  await expect(baseContent).toHaveValue(multilineContent);

  // No modal yet.
  const modalOverlay = card.locator(".memory-proposal-modal__overlay");
  await expect(modalOverlay).toHaveCount(0);

  // Click "Expand" -- the first button in the base card's action row.
  await baseActions.locator("button").first().click();

  // The card itself is still the same, unresolved request: still attached,
  // still visible, still showing the same fields -- Expand did not resolve
  // or dismiss it.
  await expect(card).toBeVisible();
  await expect(baseContent).toBeVisible();
  await expect(baseContent).toHaveValue(multilineContent);

  // New content became visible: the modal, with its own larger view of the
  // same fields.
  await expect(modalOverlay).toBeVisible();
  const modalContent = modalOverlay.locator(".memory-proposal-card__content");
  await expect(modalContent).toBeVisible();
  await expect(modalContent).toHaveAttribute("rows", "18");
  await expect(modalContent).toHaveValue(multilineContent);
  // The name field is duplicated into the modal too, confirming this is the
  // full field set re-rendered larger, not just the content box.
  await expect(
    modalOverlay.locator(".memory-proposal-card__field input").first(),
  ).toHaveValue("expand-test-memory");

  // The 4-row base textarea cannot show all 10 lines at once (its scrollable
  // content exceeds its visible box); the 18-row modal textarea can. This is
  // the concrete "additional detail visible" Expand provides.
  const [baseOverflow, modalOverflow] = await Promise.all([
    baseContent.evaluate((el: HTMLTextAreaElement) => el.scrollHeight - el.clientHeight),
    modalContent.evaluate((el: HTMLTextAreaElement) => el.scrollHeight - el.clientHeight),
  ]);
  expect(baseOverflow).toBeGreaterThan(0);
  expect(modalOverflow).toBeLessThanOrEqual(0);

  // Collapse back (clicking outside/"Collapse" -- also not a resolve) and
  // confirm the card is still open and unresolved.
  await modalOverlay.locator(".user-input-card__actions button").first().click();
  await expect(modalOverlay).toHaveCount(0);
  await expect(card).toBeVisible();

  // Now approve normally, exactly like every other test in this file.
  await card.locator(".user-input-card__actions button.primary").click();
  await expect(card).not.toBeVisible({ timeout: 15_000 });

  const memoriesRes = await page.request.get(`${backend.baseURL}/api/memory/all`);
  expect(memoriesRes.ok()).toBeTruthy();
  const memoriesBody = await memoriesRes.json();
  const globalMemories = memoriesBody.global as Array<{ name: string; content: string }>;
  const persisted = globalMemories.find((m) => m.name === "expand-test-memory");
  expect(persisted).toBeDefined();
  expect(persisted?.content).toBe(multilineContent);
});
