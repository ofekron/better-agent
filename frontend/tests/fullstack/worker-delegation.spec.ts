import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates real fork/delegation: forking from an existing turn spawns a
// real child session, runs a real provider turn in it, and the parent's
// ForkSplitView renders both panes.
test("forks from an existing turn into a real child session", async ({ authedPage: page }) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  // ForkSplitView always includes the root/original session as its own
  // fork-pane too (see the next test's comment), so a bare fork-pane
  // locator matches 2 elements here — scope to the new fork's reply text.
  const forkPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILD" });
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });
});

// Validates that two forks spawned from the SAME parent turn both land as
// their own side-by-side panes in the split grid, and that the pre-fork
// history (the parent turn) is rendered once — shared above the split —
// rather than duplicated per pane. ForkSplitView.tsx always includes the
// root/original session as a pane too (see its `flatPanes` flattening), so
// `fork-pane` alone would match 3 elements here (root + 2 forks); the
// assertions below scope to the two new fork panes by their distinct reply
// text instead of asserting a raw count across all panes.
test("forks twice from the same parent turn into side-by-side panes sharing one history", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  const forkGrid = page.getByTestId("fork-grid");

  // First fork off the parent turn.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: BRANCH_A.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  await expect(
    forkGrid.getByTestId("fork-pane").filter({ hasText: "BRANCH_A" }).getByTestId("assistant-message"),
  ).toContainText("BRANCH_A", { timeout: 120_000 });

  // Switch focus back to the original pane (always the first tab — see
  // flatPanes' depth-first root-then-forks order) so the second fork
  // branches from the same parent turn rather than from fork A.
  await page.getByTestId("fork-tabs-strip").getByRole("tab").first().click();

  // Second fork off the SAME parent turn.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: BRANCH_B.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(
    forkGrid.getByTestId("fork-pane").filter({ hasText: "BRANCH_B" }).getByTestId("assistant-message"),
  ).toContainText("BRANCH_B", { timeout: 120_000 });

  // Creating a fork switches the view into a single-pane "focused view"
  // (the other panes stay in the DOM at zero width); expand back to the
  // full grid so both new forks are genuinely rendered side by side.
  await page.getByTestId("fork-back-to-split").click();

  const newForkPanes = forkGrid.getByTestId("fork-pane").filter({ hasText: /BRANCH_A|BRANCH_B/ });
  await expect(newForkPanes).toHaveCount(2);
  await expect(newForkPanes.filter({ hasText: "BRANCH_A" })).toBeVisible();
  await expect(newForkPanes.filter({ hasText: "BRANCH_B" })).toBeVisible();

  // The pre-fork history (the PARENT turn) is rendered once, above the
  // split, and shared identically by both panes rather than duplicated.
  const sharedHistory = page.getByTestId("fork-shared");
  await expect(sharedHistory).toHaveCount(1);
  await expect(sharedHistory).toContainText("PARENT");
});

// Validates depth-2 forking: forking again from an already-forked pane's
// own composer (a fork of a fork). ForkSplitView has no per-pane
// InputArea — Chat.tsx mounts exactly one shared InputArea whose Fork
// action (App.tsx's handleForkAndSend) always targets `currentSession`,
// which resolves via `focusedForkId` (see App.tsx around line 857). So
// `fork-btn` behaves identically whether the focused pane is the root or
// a fork — no special interaction is needed to fork "from within" a fork
// pane, beyond making sure that pane is the focused one.
//
// Tokens are chosen so none is a substring of another (unlike e.g.
// "CHILD"/"GRANDCHILD") so `hasText` filters can't cross-match.
test("forks a second time from an already-forked child into a depth-2 grandchild", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  const forkGrid = page.getByTestId("fork-grid");

  // First fork: root -> CHILDFORK.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILDFORK.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const childPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILDFORK" });
  await expect(childPane.getByTestId("assistant-message")).toContainText("CHILDFORK", {
    timeout: 120_000,
  });

  // Forking auto-focuses the new child (handleForkAndSend calls
  // setFocusedForkId(child.id) immediately), but switch to its tab
  // explicitly so the second fork is unambiguously issued from WITHIN
  // the child pane rather than relying on that implicit focus. Tab
  // order mirrors flatPanes' depth-first root-then-forks order, so
  // index 1 is CHILDFORK.
  await page.getByTestId("fork-tabs-strip").getByRole("tab").nth(1).click();

  // Second fork, issued from the same shared composer while CHILDFORK
  // is focused: CHILDFORK -> GRANDFORK.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: GRANDFORK.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const grandchildPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "GRANDFORK" });
  await expect(grandchildPane.getByTestId("assistant-message")).toContainText("GRANDFORK", {
    timeout: 120_000,
  });

  // Expand out of the post-fork focused single-pane view (see the
  // two-forks test's comment) so every pane is genuinely rendered.
  await page.getByTestId("fork-back-to-split").click();

  // The grandchild's own reply is distinct from its ancestors' turns.
  await expect(grandchildPane).not.toContainText("PARENT");

  // The root's own turn (PARENT) is the tree's earliest fork point, so
  // it renders once, shared above the split — never duplicated per pane.
  const sharedHistory = page.getByTestId("fork-shared");
  await expect(sharedHistory).toHaveCount(1);
  await expect(sharedHistory).toContainText("PARENT");
  await expect(sharedHistory).not.toContainText("CHILDFORK");

  // The grandchild copied CHILDFORK's full history (PARENT + CHILDFORK)
  // at ITS fork time. Per-pane splitting uses the EARLIEST fork point
  // in the whole tree (root -> CHILDFORK's), so CHILDFORK's own turn
  // falls after that boundary and renders inside the grandchild's own
  // pane as inherited ancestor history, alongside its distinct new reply.
  await expect(grandchildPane).toContainText("CHILDFORK");
});

// Validates that fork-btn is genuinely disabled — not just styled to look
// disabled — when the session has no valid fork source yet.
// InputArea.tsx's fork button (line ~1198) is
// `disabled={!localDraft.trim() || disabled || !canFork}`; `canFork` is
// wired from App.tsx's `currentSessionCanFork`, which is
// `sessionHasForkSource(currentSession) && ...` (App.tsx ~1998), and
// `sessionHasForkSource` (utils/sessionFork.ts) is `Boolean(session?.
// agent_session_id)` — the real provider CLI session id, only populated
// once a turn has actually run. So a session with zero completed turns has
// no fork source structurally, regardless of draft content.
//
// To get that state without racing turn-completion timing, this uses the
// New Session modal's "Create" action (no send) instead of the usual
// "Create & Send & Open" — it creates a session with zero turns and does
// NOT navigate into it, so the session is opened manually from the
// sidebar afterward.
test("fork-btn is disabled with no completed turns and toggles with the draft once one exists", async ({
  authedPage: page,
}) => {
  await page.locator(".session-new-button").click();
  const promptBox = page.getByTestId("new-session-prompt-textarea");
  await promptBox.waitFor({ state: "visible", timeout: 10_000 });

  // Open the create-action dropdown and pick "Create" (create only, no
  // send, no auto-open) so the new session genuinely has zero turns.
  await page.locator(".ns-create-toggle").click();
  await page.getByRole("menuitem", { name: "Create", exact: true }).click();
  await promptBox.waitFor({ state: "hidden", timeout: 10_000 });

  // Empty sessions float to the top of the sidebar, so the freshly created
  // zero-turn session is the first item.
  await page.getByTestId("session-item").first().click();
  await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

  // Non-empty draft, but zero completed turns: fork-btn must stay disabled
  // because there is no fork source, not because the draft is empty.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: FORKSOURCE.");
  await page.locator(".input-overflow-trigger").click();
  await expect(page.getByTestId("fork-btn")).toBeDisabled();

  // Send the real first turn (closes the overflow menu as a side effect,
  // since the click lands outside .input-overflow-wrapper).
  await page.getByTestId("send-btn").click();
  await expect(page.getByTestId("assistant-message")).toContainText("FORKSOURCE", {
    timeout: 120_000,
  });

  // Now that a real turn has completed, a non-empty draft makes fork-btn
  // enabled.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: NEWFORK.");
  await page.locator(".input-overflow-trigger").click();
  await expect(page.getByTestId("fork-btn")).toBeEnabled();

  // Clearing the draft disables it again, even though a valid fork source
  // now exists.
  await page.getByTestId("input-textarea").fill("");
  await expect(page.getByTestId("fork-btn")).toBeDisabled();
});

// Validates the OTHER half of fork-btn's disabled expression independently
// of the previous test: `disabled={!localDraft.trim() || disabled ||
// !canFork}` (InputArea.tsx ~1198) has two conditions guarding it, and the
// previous test only exercised `!canFork` (no fork source at all). This
// test holds a valid fork source constant (a real completed turn, so
// `canFork` is true throughout) and toggles only `localDraft.trim()`, to
// confirm the empty-draft half is enforced on its own rather than being
// incidentally satisfied by the fork-source check.
test("fork-btn is disabled with an empty draft even when a valid fork source exists", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  // Empty composer, valid fork source (the PARENT turn already completed):
  // fork-btn must stay disabled because the draft is empty, not because
  // there's no fork source.
  await page.getByTestId("input-textarea").fill("");
  await page.locator(".input-overflow-trigger").click();
  await expect(page.getByTestId("fork-btn")).toBeDisabled();

  // Typing a draft flips it enabled, with the same fork source still valid.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await expect(page.getByTestId("fork-btn")).toBeEnabled();

  // Clearing the draft again disables it, even though the fork source is
  // still valid throughout.
  await page.getByTestId("input-textarea").fill("");
  await expect(page.getByTestId("fork-btn")).toBeDisabled();
});

// Validates what the fork pane's "x" close affordance (ForkPane's
// `fork-pane-close` button, ForkSplitView.tsx ~line 626) actually does.
// Read from source (not guessed): App.tsx's `handleCloseFork` only POSTs
// `${API}/api/sessions/${id}/close_fork`, which backend/main.py's
// `close_fork` route resolves to `session_manager.set_fork_closed(id, true)`
// — a metadata flip (`fork_closed: true`) on the still-live session record,
// not a delete. So this is "hide/freeze in place", not "remove": the pane
// stays mounted in the grid (ForkSplitView never filters `flatPanes` by
// `fork_closed`) but re-renders with a `fork-pane-closed` class, its
// focus-radio/expand controls swapped for Reopen + Delete buttons, and the
// close ("x") button itself gone (JSX gates it on `!isClosed`). The
// underlying session is NOT deleted server-side: GET /api/sessions/{id}
// (root or fork id — both resolve to the same root tree, per its own
// docstring) still returns the fork embedded in the root's `forks` array,
// now carrying `fork_closed: true`. Note forks are never separate entries
// in the flat GET /api/sessions LIST endpoint regardless of open/closed
// state (confirmed in backend/scripts/test_fork_split.py, which asserts
// fork ids are excluded from that list even while open) — so that endpoint
// isn't a useful signal here; the tree-detail endpoint is.
test("closing a fork pane freezes it in place (still visible, session not deleted) rather than removing it from the grid", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const forkPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILD" });
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });

  const forkSessionId = await forkPane.getAttribute("data-session-id");
  if (!forkSessionId) throw new Error("fork pane is missing data-session-id");

  // Expand out of the post-fork focused single-pane view so the grid (and
  // the close button, which only exists in the grid layout) is genuinely
  // rendered — mirrors the pattern used by the other fork tests above.
  await page.getByTestId("fork-back-to-split").click();

  await forkPane.locator(".fork-pane-close").click();

  // The pane does NOT disappear from the grid — it stays mounted, just
  // marked closed. Same locator (by CHILD text) still resolves to exactly
  // one element, and it now carries the closed styling with Reopen/Delete
  // controls instead of the close ("x") button.
  await expect(forkPane).toBeVisible();
  await expect(forkPane).toHaveClass(/fork-pane-closed/);
  await expect(forkPane.locator(".fork-pane-close")).toHaveCount(0);
  await expect(forkPane.locator(".fork-pane-reopen")).toBeVisible();
  await expect(forkPane.locator(".fork-pane-delete")).toBeVisible();

  // The underlying forked session itself is NOT deleted server-side — the
  // fork node is still embedded in the root tree, just flagged fork_closed.
  interface TreeNode {
    id: string;
    fork_closed?: boolean;
    forks?: TreeNode[];
  }
  const findNode = (node: TreeNode, id: string): TreeNode | null => {
    if (node.id === id) return node;
    for (const f of node.forks ?? []) {
      const found = findNode(f, id);
      if (found) return found;
    }
    return null;
  };

  const res = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(forkSessionId)}?msg_limit=5`,
  );
  expect(res.ok()).toBe(true);
  const tree = (await res.json()) as TreeNode;
  const forkNode = findNode(tree, forkSessionId);
  if (!forkNode) throw new Error(`fork session ${forkSessionId} not found in root tree — was deleted`);
  expect(forkNode.fork_closed).toBe(true);
});

// Validates the other half of the close/reopen lifecycle: clicking the real
// "Reopen" button (ForkPane's `fork-pane-reopen` button, ForkSplitView.tsx
// ~line 557) flips the pane back to its normal interactive state. App.tsx's
// `handleReopenFork` POSTs `${API}/api/sessions/${id}/reopen_fork`, which
// backend/main.py resolves to `session_manager.set_fork_closed(id, false)` —
// the mirror image of close_fork. So after reopening, the pane should lose
// its `fork-pane-closed` class, regain its normal controls (the `!isClosed`
// branch renders `fork-pane-view-button` for expand and `fork-pane-focus-radio`
// for focus, plus `fork-pane-close` for the "x"), lose the Reopen/Delete
// buttons (gated on `isClosed`), and the session detail endpoint should
// report `fork_closed: false` again.
test("reopening a closed fork pane via the Reopen button restores it to normal, interactive state", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const forkPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILD" });
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });

  const forkSessionId = await forkPane.getAttribute("data-session-id");
  if (!forkSessionId) throw new Error("fork pane is missing data-session-id");

  await page.getByTestId("fork-back-to-split").click();

  await forkPane.locator(".fork-pane-close").click();
  await expect(forkPane).toHaveClass(/fork-pane-closed/);
  await expect(forkPane.locator(".fork-pane-reopen")).toBeVisible();

  interface TreeNode {
    id: string;
    fork_closed?: boolean;
    forks?: TreeNode[];
  }
  const findNode = (node: TreeNode, id: string): TreeNode | null => {
    if (node.id === id) return node;
    for (const f of node.forks ?? []) {
      const found = findNode(f, id);
      if (found) return found;
    }
    return null;
  };

  const closedRes = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(forkSessionId)}?msg_limit=5`,
  );
  expect(closedRes.ok()).toBe(true);
  const closedTree = (await closedRes.json()) as TreeNode;
  const closedNode = findNode(closedTree, forkSessionId);
  if (!closedNode) throw new Error(`fork session ${forkSessionId} not found in root tree`);
  expect(closedNode.fork_closed).toBe(true);

  // Click the real Reopen button.
  await forkPane.locator(".fork-pane-reopen").click();

  // The pane loses its closed styling and the Reopen/Delete controls...
  await expect(forkPane).not.toHaveClass(/fork-pane-closed/);
  await expect(forkPane.locator(".fork-pane-reopen")).toHaveCount(0);
  await expect(forkPane.locator(".fork-pane-delete")).toHaveCount(0);

  // ...and regains its normal interactive controls: expand, focus radio,
  // and the close ("x") button.
  await expect(forkPane.locator(".fork-pane-view-button")).toBeVisible();
  await expect(forkPane.locator(".fork-pane-focus-radio")).toBeVisible();
  await expect(forkPane.locator(".fork-pane-close")).toBeVisible();

  // The server-side record reflects the reopen too.
  const reopenedRes = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(forkSessionId)}?msg_limit=5`,
  );
  expect(reopenedRes.ok()).toBe(true);
  const reopenedTree = (await reopenedRes.json()) as TreeNode;
  const reopenedNode = findNode(reopenedTree, forkSessionId);
  if (!reopenedNode) throw new Error(`fork session ${forkSessionId} not found in root tree`);
  expect(reopenedNode.fork_closed).toBe(false);
});

// Validates fork/session identity: a fork gets its own real session record
// distinct from its parent, reachable via the tree-detail endpoint, but is
// NOT a separate top-level entry in the flat list endpoint.
//
// Read from source, not guessed:
//   - `backend/session_store.py` `list_sessions()` (~line 5598) docstrings
//     this explicitly: "Forks are NOT included as top-level entries —
//     they're embedded in each root's `forks` array." `GET /api/sessions`
//     (backend/main.py) wraps that function directly, and
//     `backend/scripts/test_fork_split.py` asserts the same invariant
//     (`list_sessions hides forks; fork_count = 2`). So a fork's id must
//     never appear in that endpoint's response — this is by design, not an
//     oversight, and this test locks that in rather than just checking the
//     positive "fork exists" case.
//   - `session_store.fork_session` (~line 5828) gives the new fork its own
//     `id` (a fresh uuid4) always, and its own `name`: by default
//     `parent_name + " (fork)"` (or `"(fork N)"` for repeat forks) — a
//     deterministically different string from the parent's, even though
//     it's derived from the PARENT's name rather than the fork's own
//     prompt (the first-prompt auto-rename in `orchestrator.py` ~line 5019
//     only fires when the session has zero messages, and a fork always
//     starts with the parent's copied messages, so that path never
//     triggers for forks).
//   - The tree endpoint `GET /api/sessions/{id}` embeds the fork inside the
//     parent's `forks` array with its own `id`, matching the id returned
//     by `fork_and_send` and rendered on the pane's `data-session-id`.
test("a fork gets its own distinct session id/name in the tree endpoint, but is excluded from the flat sessions list", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });
  const parentId = new URL(page.url()).pathname.replace(/^\/s\//, "");

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const forkPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILD" });
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });

  const forkSessionId = await forkPane.getAttribute("data-session-id");
  if (!forkSessionId) throw new Error("fork pane is missing data-session-id");
  expect(forkSessionId).not.toBe(parentId);

  // Tree-detail endpoint: the fork is embedded in the parent's tree as its
  // own node, with its own id and a name distinct from the parent's.
  interface TreeNode {
    id: string;
    name?: string;
    forks?: TreeNode[];
  }
  const findNode = (node: TreeNode, id: string): TreeNode | null => {
    if (node.id === id) return node;
    for (const f of node.forks ?? []) {
      const found = findNode(f, id);
      if (found) return found;
    }
    return null;
  };

  const treeRes = await page.request.get(
    `${backend.baseURL}/api/sessions/${encodeURIComponent(parentId)}?msg_limit=5`,
  );
  expect(treeRes.ok()).toBe(true);
  const tree = (await treeRes.json()) as TreeNode;
  expect(tree.id).toBe(parentId);

  const forkNode = findNode(tree, forkSessionId);
  if (!forkNode) throw new Error(`fork session ${forkSessionId} not found in parent's tree`);
  expect(forkNode.id).not.toBe(tree.id);
  expect(forkNode.name).toBeTruthy();
  expect(forkNode.name).not.toBe(tree.name);

  // Cross-check: the fork also shows up directly in the parent's own
  // `forks` array (not just reachable via tree-walk).
  expect((tree.forks ?? []).some((f) => f.id === forkSessionId)).toBe(true);

  // Flat list endpoint: the parent (a root) is a top-level entry, but the
  // fork is deliberately excluded — it's not a bug, `list_sessions()` says
  // so explicitly.
  interface SessionListItem {
    id: string;
  }
  const listRes = await page.request.get(`${backend.baseURL}/api/sessions?limit=20`);
  expect(listRes.ok()).toBe(true);
  const listBody = (await listRes.json()) as { sessions?: SessionListItem[] } | SessionListItem[];
  const items = Array.isArray(listBody) ? listBody : listBody.sessions ?? [];
  expect(items.some((s) => s.id === parentId)).toBe(true);
  expect(items.some((s) => s.id === forkSessionId)).toBe(false);
});
